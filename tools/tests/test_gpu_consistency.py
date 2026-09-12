"""GPU-only compatibility probes; no browser, network or Chromium build.

Sparse-chain probes copy local pinned inputs. C++ probes execute applied/extracted
functions with GL/config stubs, not a GPU backend. The optional seed hash probe
links the locally available Chromium SuperFastHash and integer parser implementations.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import subprocess

import pytest

import test_fingerprint_gpu as gpu

ROOT = Path(__file__).resolve().parents[2]
BASELINES = Path(os.environ.get(
    "CHROMIX_GPU_SPARSE_ROOT", "/tmp/chromix-merge-added-hwbqz4re/fixtures"))
WEBGL = "third_party/blink/renderer/modules/webgl/webgl_rendering_context_base.cc"
WEBGL2 = "third_party/blink/renderer/modules/webgl/webgl2_rendering_context_base.cc"
FORMAT = "third_party/blink/renderer/modules/webgpu/gpu.cc"


def patch_path(number):
    return next((ROOT / "patches").glob(f"{number}-*.patch"))


def apply(directory, patch, *, reverse=False):
    if gpu.PATCH_BIN is None:
        pytest.skip("GNU patch unavailable")
    command = [gpu.PATCH_BIN, "-p1", "--fuzz=0", "--batch", "--get=0",
               "--no-backup-if-mismatch", "--reject-file=-", "-i", str(patch)]
    if reverse:
        command.append("--reverse")
    result = subprocess.run(command, cwd=directory, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "fuzz" not in result.stdout + result.stderr


def compile_cpp(directory, source, *flags):
    if gpu.CXX is None:
        pytest.skip("local C++20 compiler unavailable")
    path = directory / "probe.cc"
    path.write_text(source)
    binary = directory / "probe"
    result = subprocess.run([gpu.CXX, "-std=c++20", "-Wall", "-Wextra", "-Werror",
                             *flags, str(path), "-o", str(binary)],
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    return binary


def execute(binary, *args):
    result = subprocess.run([str(binary), *map(str, args)], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout


def function(source, name):
    begin = source.index(name)
    brace = source.index("{", begin)
    depth = 1
    end = brace + 1
    while depth:
        depth += (source[end] == "{") - (source[end] == "}")
        end += 1
    return source[begin:end]


def tokens(source):
    return re.sub(r"\s+", "", re.sub(r"//[^\n]*", "", source))


@pytest.fixture(scope="module", params=["linux", "macos", "windows"])
def sparse_gpu_sources(request, tmp_path_factory):
    baseline = BASELINES / request.param / "upstream"
    if not (baseline / WEBGL).exists():
        pytest.skip("supply CHROMIX_GPU_SPARSE_ROOT with pinned per-platform upstream trees")
    directory = tmp_path_factory.mktemp(f"gpu-chain-{request.param}")
    originals = {}
    states = {}
    for target in (WEBGL, WEBGL2, FORMAT):
        path = baseline / target
        originals[target] = path.read_text()
        states[target] = (hashlib.sha256(path.read_bytes()).digest(), path.stat().st_mtime_ns)
        copy = directory / target
        copy.parent.mkdir(parents=True, exist_ok=True)
        copy.write_bytes(path.read_bytes())
    for patch in sorted((ROOT / "patches").glob("*.patch")):
        target = re.search(r"^\+\+\+ b/(.*)$", patch.read_text(), re.M)[1]
        if target in originals:
            apply(directory, patch)
    patched = {target: (directory / target).read_text() for target in originals}
    for target, state in states.items():
        path = baseline / target
        assert (hashlib.sha256(path.read_bytes()).digest(), path.stat().st_mtime_ns) == state
    return originals, patched


def test_readback_chain_preserves_all_native_entry_points(sparse_gpu_sources):
    original, patched = sparse_gpu_sources
    for name in ("bool WebGLRenderingContextBase::ValidateReadPixelsFuncParameters",
                 "void WebGLRenderingContextBase::ReadPixelsHelper"):
        assert tokens(function(original[WEBGL], name)) == tokens(function(patched[WEBGL], name))
    for name in ("WebGL2RenderingContextBase::readPixels", "WebGL2RenderingContextBase::GetPackPixelStoreParams"):
        before = original[WEBGL2].split(name)
        after = patched[WEBGL2].split(name)
        assert len(before) == len(after)
        for left, right in zip(before[1:], after[1:]):
            assert tokens(function(name + left, name)) == tokens(function(name + right, name))
    helper = function(patched[WEBGL], "void WebGLRenderingContextBase::ReadPixelsHelper")
    for forbidden in ("memcpy", "ApplyWebGLReadbackNoise", "GetImageDataCacheFirst", "GetError"):
        assert forbidden not in helper


def test_preferred_format_chain_stays_native(sparse_gpu_sources):
    original, patched = sparse_gpu_sources
    name = "WebGLShaderPrecisionFormat* WebGLRenderingContextBase::getShaderPrecisionFormat"
    assert tokens(function(original[WEBGL], name)) == tokens(function(patched[WEBGL], name))
    assert "ExtensionSupportedAndAllowed(tracker)" in function(
        patched[WEBGL], "WebGLRenderingContextBase::getSupportedExtensions")
    assert "ExtensionSupportedAndAllowed(tracker)" in function(
        patched[WEBGL], "WebGLRenderingContextBase::EnableExtensionIfSupported")
    name = "wgpu::TextureFormat GPU::GetPreferredCanvasFormat"
    assert tokens(function(original[FORMAT], name)) == tokens(function(patched[FORMAT], name))
    assert "uxr-webgpu-canvas-format" not in patched[FORMAT]


@pytest.mark.parametrize("platform", ["linux", "android", "windows", "macos"])
def test_preferred_format_executable_ignores_incompatible_override(tmp_path, platform):
    native = """}

wgpu::TextureFormat GPU::GetPreferredCanvasFormat() {
#if BUILDFLAG(IS_ANDROID) || BUILDFLAG(IS_LINUX)
  // Interop of vulkan and GL has mesa driver bugs for BGRA format
  // See anglebug.com/40644739
  return wgpu::TextureFormat::RGBA8Unorm;
#else
  return wgpu::TextureFormat::BGRA8Unorm;
#endif
}
"""
    path = tmp_path / FORMAT
    path.parent.mkdir(parents=True)
    preimage = "\n" * 416 + native
    path.write_text(preimage)
    apply(tmp_path, patch_path("0114"))
    patched = function(path.read_text(), "wgpu::TextureFormat GPU::GetPreferredCanvasFormat")
    apply(tmp_path, patch_path("0114"), reverse=True)
    assert path.read_text() == preimage
    source = gpu.CPP_SUPPORT[:gpu.CPP_SUPPORT.index("namespace wgpu {")] + """
#define BUILDFLAG(x) x
namespace wgpu { enum class TextureFormat { RGBA8Unorm, BGRA8Unorm }; }
struct GPU { static wgpu::TextureFormat GetPreferredCanvasFormat(); };
""" + patched + r'''
int main() {
  const auto native = (IS_LINUX || IS_ANDROID) ? wgpu::TextureFormat::RGBA8Unorm
                                               : wgpu::TextureFormat::BGRA8Unorm;
  assert(GPU::GetPreferredCanvasFormat() == native);
  for (const char* value : {"rgba8unorm", "bgra8unorm", "", "invalid", "RGBA8Unorm"}) {
    base::UxrConfig::GetInstance().values["uxr-webgpu-canvas-format"] = value;
    assert(GPU::GetPreferredCanvasFormat() == native);
  }
}
'''
    execute(compile_cpp(tmp_path, source, f"-DIS_LINUX={int(platform == 'linux')}",
                        f"-DIS_ANDROID={int(platform == 'android')}"))


@pytest.fixture(scope="module")
def seed_sources(tmp_path_factory):
    directory = tmp_path_factory.mktemp("farble-source")
    result = {}
    for number in ("0105", "0106"):
        patch = patch_path(number)
        target = re.search(r"^\+\+\+ b/(.*)$", patch.read_text(), re.M)[1]
        path = directory / target
        path.parent.mkdir(parents=True, exist_ok=True)
        apply(directory, patch)
        result[number] = path.read_text()
        apply(directory, patch, reverse=True)
        assert not path.exists()
    return result


@pytest.fixture(scope="module", params=["stub-hash", "chromium-hash"])
def seed_binary(request, tmp_path_factory, seed_sources):
    directory = tmp_path_factory.mktemp(f"farble-{request.param}")
    support = gpu.CPP_SUPPORT[:gpu.CPP_SUPPORT.index("namespace wgpu {")]
    if request.param == "chromium-hash":
        source = ROOT / ".chromix-build-verify/src/base/third_party/superfasthash/superfasthash.c"
        if not source.exists():
            pytest.skip("local Chromium SuperFastHash implementation unavailable")
        support += '\n' + source.read_text()
        parser_path = ROOT / ".chromix-build-verify/src/base/strings/string_number_conversions_internal.h"
        parser = parser_path.read_text()
        parser = parser[parser.index("template <int BASE, typename CHAR>"):
                        parser.index("template <typename T, typename VALUE, typename CharT = typename T::value_type>\nbool HexStringToIntImpl")]
        declaration = "#include <string>\n#include <cstdint>\nnamespace base { bool StringToUint64(const std::string&, uint64_t*); }\n"
        support = declaration + support.replace(
            "bool GetUint64(const std::string& key, uint64_t* out) const {\n    return GetNumber(key, out);\n  }",
            "bool GetUint64(const std::string& key, uint64_t* out) const {\n    return Has(key) && StringToUint64(Get(key), out);\n  }")
        support += "\nnamespace base::internal {\nbool IsAsciiWhitespace(char c) { return c == ' ' || (c >= '\\t' && c <= '\\r'); }\n" + parser + "}\n"
        support += "namespace base { bool StringToUint64(const std::string& text, uint64_t* out) { return internal::StringToIntImpl(std::string_view(text), *out); } }\n"
        hash_body = "return SuperFastHash(reinterpret_cast<const char*>(data.data()), static_cast<int>(data.size()));"
    else:
        hash_body = "uint32_t hash = 2166136261u; for (uint8_t byte : data) hash = (hash ^ byte) * 16777619u; return hash;"
    support += """
#include <array>
#include <thread>
#include <iostream>
namespace base {
uint32_t PersistentHash(span<const uint8_t> data) { HASH_BODY }
uint32_t PersistentHash(std::string_view text) {
  return PersistentHash(span(reinterpret_cast<const uint8_t*>(text.data()), text.size()));
}
std::string NumberToString(uint64_t value) { return std::to_string(value); }
}
""".replace("HASH_BODY", hash_body)
    seed = "\n".join(line for line in seed_sources["0105"].splitlines() if not line.startswith("#include"))
    flags = ["-pthread", "-fsanitize=undefined", "-fno-sanitize-recover=all"]
    if os.name == "nt":
        flags.append("-fms-runtime-lib=static")
        # MSVC's library search precedes Clang's resource directory on Windows.
        # Its identically named UBSan library belongs to a different toolchain
        # and can reference unavailable ASan COE symbols. Bind this compiler's
        # own runtimes explicitly; keep UBSan enabled rather than skipping it.
        resource = subprocess.run([gpu.CXX, "-print-resource-dir"],
                                  capture_output=True, text=True, timeout=10)
        assert resource.returncode == 0, resource.stderr
        runtime = Path(resource.stdout.strip()) / "lib" / "windows"
        for name in ("clang_rt.ubsan_standalone-x86_64.lib",
                     "clang_rt.ubsan_standalone_cxx-x86_64.lib"):
            library = runtime / name
            assert library.is_file(), f"missing compiler-matched UBSan runtime: {library}"
            flags.append(str(library))
    return compile_cpp(directory, support + seed + SEED_TESTS, *flags)


@pytest.mark.parametrize("case", ["bits", "legacy", "invalid", "precedence", "lifecycle", "threads"])
def test_full_width_seed_contract(seed_binary, case):
    assert execute(seed_binary, case) == execute(seed_binary, case)


SEED_TESTS = r'''
uint64_t LegacySeed(uint32_t seed, std::string_view site) {
  uint32_t high = base::PersistentHash(site) + seed - 1u;
  std::array<uint8_t, 4> bytes{};
  for (size_t i = 0; i < bytes.size(); ++i) bytes[i] = (high >> (i * 8)) & 255;
  return (uint64_t{high} << 32) ^ base::PersistentHash(base::span(bytes)) ^ (uint64_t{seed} << 16);
}
int main(int argc, char** argv) {
  assert(argc == 2);
  auto& config = base::UxrConfig::GetInstance();
  const std::string mode = argv[1];
  auto seed = [&] { return ungoogled::GetFarbleSeed64("example.test"); };
  if (mode == "bits") {
    std::set<uint64_t> values;
    for (unsigned bit = 0; bit < 64; ++bit) {
      config.values["uxr-fingerprint-seed"] = std::to_string(uint64_t{1} << bit);
      assert(ungoogled::GlobalSeed() == (uint64_t{1} << bit));
      assert(values.insert(seed()).second);
    }
    for (uint64_t high : {uint64_t{1}, uint64_t{0x80000000}, uint64_t{UINT32_MAX}}) {
      for (uint32_t low : {0u, 1u, 17u, UINT32_MAX}) {
        config.values["uxr-fingerprint-seed"] = std::to_string((high << 32) | low);
        assert(seed() == (LegacySeed(low, "example.test") ^ (high * 0x9e3779b97f4a7c15ULL)));
        assert(seed() != LegacySeed(low, "example.test"));
      }
    }
  } else if (mode == "legacy") {
    for (uint32_t value : {0u, 1u, 17u, 0x80000000u, UINT32_MAX}) {
      config.values["uxr-fingerprint-seed"] = std::to_string(value);
      for (const char* domain : {"", "example.test", "other.test"})
        assert(ungoogled::GetFarbleSeed64(domain) == LegacySeed(value, domain));
    }
    assert(ungoogled::GetFarbleSeed64("example.test") != ungoogled::GetFarbleSeed64("other.test"));
  } else if (mode == "invalid") {
    config.values["uxr-canvas-seed"] = "17";
    for (const std::string raw : {"-1", "-0", "abc", "12x", " 12", "12 ", "0x10",
                                  "++1", "+-1", "1.0", "1e2", "18446744073709551616"}) {
      config.values["uxr-fingerprint-seed"] = raw;
      assert(ungoogled::GlobalSeed() == 0 && seed() == LegacySeed(0, "example.test"));
    }
    config.values["uxr-fingerprint-seed"] = std::string("17\0suffix", 9);
    assert(ungoogled::GlobalSeed() == 0);
    for (const char* raw : {"+17", "00017"}) {
      config.values["uxr-fingerprint-seed"] = raw;
      assert(ungoogled::GlobalSeed() == 17);
    }
  } else if (mode == "precedence") {
    config.values["uxr-canvas-seed"] = "18446744073709551615";
    assert(ungoogled::GlobalSeed() == UINT64_MAX);
    config.values["uxr-fingerprint-seed"] = "";
    assert(ungoogled::GlobalSeed() == UINT64_MAX);
    config.values["uxr-fingerprint-seed"] = "0";
    assert(ungoogled::GlobalSeed() == 0);
    config.values["uxr-fingerprint-seed"] = "4294967296";
    assert(ungoogled::GlobalSeed() == (uint64_t{1} << 32));
  } else if (mode == "lifecycle") {
    const uint64_t startup = seed();
    assert(ungoogled::FingerprintNoiseEnabled());
    config.values = {{"uxr-fingerprint-seed", "4294967296"}, {"uxr-disable-fingerprint-noise", ""}};
    assert(seed() != startup && !ungoogled::FingerprintNoiseEnabled());
    const uint64_t initialized = seed();
    config.values.erase("uxr-disable-fingerprint-noise");
    assert(seed() == initialized && ungoogled::FingerprintNoiseEnabled());
    config.values.clear();
    assert(seed() == startup);
  } else if (mode == "threads") {
    config.values["uxr-fingerprint-seed"] = "18446744073709551615";
    const uint64_t expected = seed();
    std::vector<std::thread> workers;
    for (int i = 0; i < 8; ++i) workers.emplace_back([&] {
      for (int j = 0; j < 1000; ++j) {
        assert(seed() == expected);
        assert(ungoogled::GetFarbleSeedString("example.test") == std::to_string(expected));
      }
    });
    for (auto& worker : workers) worker.join();
  } else { assert(false); }
  std::cout << seed() << '\n';
}
'''


@pytest.fixture(scope="module")
def readback_binary(tmp_path_factory, sparse_gpu_sources):
    _, patched = sparse_gpu_sources
    directory = tmp_path_factory.mktemp("gpu-readback")
    helpers = "\n".join(function(patched[WEBGL], name) for name in (
        "bool WebGLRenderingContextBase::ValidateReadPixelsFuncParameters",
        "void WebGLRenderingContextBase::ReadPixelsHelper"))
    return compile_cpp(directory, READBACK_SUPPORT + helpers + READBACK_TESTS,
                       "-fsanitize=address,undefined", "-fno-omit-frame-pointer")


@pytest.mark.parametrize("case", ["pack", "errors", "bounds", "lifecycle"])
def test_extracted_readback_preserves_gl_contract(readback_binary, case):
    execute(readback_binary, case)


@pytest.fixture(scope="module")
def capability_binary(tmp_path_factory, sparse_gpu_sources):
    _, patched = sparse_gpu_sources
    directory = tmp_path_factory.mktemp("gpu-capability")
    helpers = []
    for number, target in enumerate((WEBGL, WEBGL2)):
        methods = "\n".join(function(patched[target], name) for name in (
            "GLint ClampPersonaLimit(", "GLfloat ClampPersonaLimitF("))
        if target == WEBGL:
            methods += function(patched[target], "std::array<GLint, 2> PersonaViewport(")
        helpers.append(f"namespace version{number} {{\n{methods}\n}}")
    helpers.append(function(patched[WEBGL], "bool WebGLRenderingContextBase::ExtensionSupportedAndAllowed"))
    return compile_cpp(directory, CAPABILITY_SUPPORT + "\n".join(helpers) + CAPABILITY_TESTS)


@pytest.mark.parametrize("case", ["limits", "extensions"])
def test_webgl_native_capability_contract(capability_binary, case):
    execute(capability_binary, case)


CAPABILITY_SUPPORT = r'''
#include <algorithm>
#include <array>
#include <cassert>
#include <cmath>
#include <limits>
#include <map>
#include <string>
#include <vector>
using GLint = int;
using GLfloat = float;
using GLenum = unsigned;
constexpr GLenum GL_MAX_VIEWPORT_DIMS = 1;
std::string Lower(std::string value) {
  for (char& c : value) if (c >= 'A' && c <= 'Z') c += 'a' - 'A';
  return value;
}
struct String {
  std::string value;
  String(const char* raw) : value(raw) {}
  explicit String(std::string raw) : value(std::move(raw)) {}
  static String FromUtf8(const std::string& raw) { return String(raw); }
};
namespace base {
struct UxrConfig {
  bool has = false;
  bool Has(const char*) const { return has; }
  static UxrConfig& GetInstance() { static UxrConfig config; return config; }
};
}
namespace ungoogled {
struct Persona { bool webgl_real = false; std::vector<std::string> webgl_extensions; };
Persona persona;
const Persona& CurrentPersona() { return persona; }
}
namespace gpu::gles2 {
struct GLES2Interface {
  GLint integer = 0;
  GLfloat floating = 0;
  std::array<int, 2> viewport{0, 0};
  void GetIntegerv(GLenum pname, GLint* output) {
    if (pname == GL_MAX_VIEWPORT_DIMS) { output[0] = viewport[0]; output[1] = viewport[1]; }
    else *output = integer;
  }
  void GetFloatv(GLenum, GLfloat* output) { *output = floating; }
};
}
struct RuntimeEnabledFeatures {
  static bool WebGLDraftExtensionsEnabled() { return false; }
  static bool WebGLDeveloperExtensionsEnabled() { return false; }
};
struct WebGLRenderingContextBase {
  struct ExtensionTracker {
    const char* name = "EXT_native";
    bool supported = true, draft = false, developer = false;
    bool Draft() const { return draft; }
    bool Developer() const { return developer; }
    bool Supported(WebGLRenderingContextBase*) const { return supported; }
    const char* ExtensionName() const { return name; }
    bool MatchesName(const String& candidate) const { return Lower(name) == Lower(candidate.value); }
  };
  struct Disabled { bool disabled = false; bool Contains(const String&) { return disabled; } } disabled_extensions_;
  bool ExtensionSupportedAndAllowed(const ExtensionTracker*);
};
'''

CAPABILITY_TESTS = r'''
int main(int argc, char** argv) {
  assert(argc == 2);
  const std::string mode = argv[1];
  if (mode == "limits") {
    gpu::gles2::GLES2Interface gl;
    for (int native : {0, 1, 16, 4096, std::numeric_limits<int>::max()}) {
      gl.integer = native;
      gl.floating = native;
      for (int value : {-1, 0, 1, 16, 8192, std::numeric_limits<int>::max()}) {
        const int expected = std::min(native, std::max(0, value));
        assert(version0::ClampPersonaLimit(&gl, 0, value) == expected);
        assert(version1::ClampPersonaLimit(&gl, 0, value) == expected);
        const float expected_f = std::min(gl.floating, std::max(0.0f, float(value)));
        assert(version0::ClampPersonaLimitF(&gl, 0, value) == expected_f);
        assert(version1::ClampPersonaLimitF(&gl, 0, value) == expected_f);
      }
      for (int other : {0, 1, 8192}) {
        gl.viewport = {native, other};
        const auto viewport = version0::PersonaViewport(&gl, 16384);
        assert(viewport[0] <= native && viewport[1] <= other);
      }
    }
  } else if (mode == "extensions") {
    WebGLRenderingContextBase context;
    WebGLRenderingContextBase::ExtensionTracker tracker;
    auto& config = base::UxrConfig::GetInstance();
    assert(context.ExtensionSupportedAndAllowed(&tracker));
    config.has = true;
    assert(!context.ExtensionSupportedAndAllowed(&tracker));
    ungoogled::persona.webgl_extensions = {"ext_native", "EXT_native", "unsupported"};
    assert(context.ExtensionSupportedAndAllowed(&tracker));
    ungoogled::persona.webgl_extensions = {std::string("EXT_native\0suffix", 17)};
    assert(!context.ExtensionSupportedAndAllowed(&tracker));
    ungoogled::persona.webgl_extensions = {"EXT_native"};
    for (int flag = 0; flag < 4; ++flag) {
      tracker.supported = flag != 0;
      tracker.draft = flag == 1;
      tracker.developer = flag == 2;
      context.disabled_extensions_.disabled = flag == 3;
      assert(!context.ExtensionSupportedAndAllowed(&tracker));
    }
    tracker = {};
    context.disabled_extensions_.disabled = false;
    ungoogled::persona.webgl_extensions.clear();
    ungoogled::persona.webgl_real = true;
    assert(context.ExtensionSupportedAndAllowed(&tracker));
    tracker.supported = false;
    assert(!context.ExtensionSupportedAndAllowed(&tracker));
  } else { assert(false); }
}
'''


READBACK_SUPPORT = r'''
#include <algorithm>
#include <cassert>
#include <cstdint>
#include <cstring>
#include <limits>
#include <optional>
#include <string>
#include <vector>
#define DCHECK(x) assert(x)
using GLint = int;
using GLsizei = int;
using GLenum = unsigned;
using GLuint = unsigned;
template <class T> using Vector = std::vector<T>;
constexpr GLenum GL_NO_ERROR = 0, GL_INVALID_VALUE = 1, GL_INVALID_OPERATION = 2,
    GL_INVALID_FRAMEBUFFER_OPERATION = 3, GL_FRAMEBUFFER_COMPLETE = 4,
    GL_RGBA = 5, GL_UNSIGNED_BYTE = 6, GL_FLOAT = 7, GL_INVALID_ENUM = 8;
constexpr size_t kMaximumSupportedArrayBufferSize = UINT32_MAX;
constexpr int kClearCallerOther = 0;
namespace base {
template <class T> struct CheckedNumeric {
  __int128_t value;
  template <class U> CheckedNumeric(U input) : value(input) {}
  template <class U> CheckedNumeric(CheckedNumeric<U> input) : value(input.value) {}
  void operator*=(size_t rhs) { value *= rhs; }
  bool IsValid() const { return value >= 0 && value <= std::numeric_limits<T>::max(); }
  T ValueOrDie() const { assert(IsValid()); return static_cast<T>(value); }
};
template <class T> CheckedNumeric<T> operator-(size_t lhs, CheckedNumeric<T> rhs) {
  return CheckedNumeric<T>(static_cast<__int128_t>(lhs) - rhs.value);
}
}
struct Pack { int alignment = 4, row_length = 0, skip_pixels = 0, skip_rows = 0; };
size_t Stride(const Pack& pack, int width, GLenum type) {
  const size_t bytes = size_t(pack.row_length ? pack.row_length : width) * (type == GL_FLOAT ? 16 : 4);
  return (bytes + pack.alignment - 1) / pack.alignment * pack.alignment;
}
size_t Skip(const Pack& pack, int width, GLenum type) {
  return Stride(pack, width, type) * pack.skip_rows + size_t(pack.skip_pixels) * (type == GL_FLOAT ? 16 : 4);
}
struct WebGLImageConversion {
  static GLenum ComputeImageSizeInBytes(GLenum, GLenum type, int width, int height,
      int, Pack pack, unsigned* bytes, void*, unsigned* skip) {
    if (width < 0 || height < 0) return GL_INVALID_VALUE;
    if (!width || !height) { *bytes = *skip = 0; return GL_NO_ERROR; }
    const uint64_t length = Stride(pack, width, type) * uint64_t(height - 1) + uint64_t(width) * (type == GL_FLOAT ? 16 : 4);
    const uint64_t skipped = Skip(pack, width, type);
    if (length > UINT32_MAX || skipped > UINT32_MAX) return GL_INVALID_VALUE;
    *bytes = length; *skip = skipped; return GL_NO_ERROR;
  }
};
struct DOMArrayBufferView {
  std::vector<uint8_t> bytes;
  size_t type_size = 1;
  explicit DOMArrayBufferView(size_t size = 512) : bytes(size, 0xa5) {}
  size_t TypeSize() const { return type_size; }
  size_t byteLength() const { return bytes.size(); }
  void* BaseAddressMaybeShared() { return bytes.empty() ? nullptr : bytes.data(); }
};
struct WebGLFramebuffer {
  bool complete = true;
  GLenum CheckDepthStencilStatus(const char**) const {
    return complete ? GL_FRAMEBUFFER_COMPLETE : GL_INVALID_FRAMEBUFFER_OPERATION;
  }
};
struct DrawingBuffer { bool bind_ok = true; };
struct ScopedDrawingBufferBinder {
  DrawingBuffer* buffer;
  ScopedDrawingBufferBinder(DrawingBuffer* input, WebGLFramebuffer*) : buffer(input) {}
  bool Succeeded() const { return buffer->bind_ok; }
};
struct HostStub { bool OriginClean() const { return true; } };
struct GL {
  Pack pack;
  bool fail = false;
  int calls = 0;
  std::vector<GLenum> errors;
  void ReadPixels(GLint x, GLint y, GLsizei width, GLsizei height, GLenum,
                  GLenum type, uint8_t* data) {
    ++calls;
    if (fail) { errors.push_back(GL_INVALID_OPERATION); return; }
    if (!width || !height) return;
    assert(data);
    const size_t pixel_size = type == GL_FLOAT ? 16 : 4;
    for (int row = 0; row < height; ++row) for (int col = 0; col < width; ++col) {
      const int64_t sx = int64_t(x) + col, sy = int64_t(y) + row;
      uint8_t* pixel = data + Skip(pack, width, type) + row * Stride(pack, width, type) + col * pixel_size;
      const bool inside = sx >= 0 && sy >= 0 && sx < 8 && sy < 8;
      for (size_t channel = 0; channel < pixel_size; ++channel)
        pixel[channel] = inside ? static_cast<uint8_t>(sx * 7 + sy * 11 + channel) : 0;
    }
  }
};
struct WebGLRenderingContextBase {
  GL gl;
  HostStub host;
  WebGLFramebuffer framebuffer;
  DrawingBuffer drawing_buffer;
  bool lost = false, lose_on_clear = false, drawing = true, bound = false;
  std::vector<GLenum> errors;
  bool isContextLost() const { return lost; }
  HostStub* Host() { return &host; }
  WebGLFramebuffer* GetReadFramebufferBinding() { return bound ? &framebuffer : nullptr; }
  DrawingBuffer* GetDrawingBuffer() { return drawing ? &drawing_buffer : nullptr; }
  GL* ContextGL() { return &gl; }
  Pack GetPackPixelStoreParams() { return gl.pack; }
  void SynthesizeGLError(GLenum error, const char*, const char*) { errors.push_back(error); }
  void ClearIfComposited(int) { lost = lost || lose_on_clear; }
  bool ValidateReadPixelsFormatAndType(GLenum format, GLenum type, DOMArrayBufferView* buffer) {
    if (format != GL_RGBA || (type != GL_UNSIGNED_BYTE && type != GL_FLOAT)) {
      SynthesizeGLError(GL_INVALID_ENUM, "", ""); return false;
    }
    if (buffer->TypeSize() != (type == GL_FLOAT ? 4u : 1u)) {
      SynthesizeGLError(GL_INVALID_OPERATION, "", ""); return false;
    }
    return true;
  }
  bool ValidateReadPixelsFuncParameters(GLsizei, GLsizei, GLenum, GLenum, DOMArrayBufferView*, int64_t);
  void ReadPixelsHelper(GLint, GLint, GLsizei, GLsizei, GLenum, GLenum, DOMArrayBufferView*, int64_t);
};
'''

READBACK_TESTS = r'''
int main(int argc, char** argv) {
  assert(argc == 2);
  const std::string mode = argv[1];
  if (mode == "pack" || mode == "bounds") {
    for (int alignment : {1, 2, 4, 8}) for (int width : {1, 3, 4})
    for (int height : {1, 2, 3}) for (int offset : {0, 1, 7})
    for (GLenum type : {GL_UNSIGNED_BYTE, GL_FLOAT}) for (int skips : {0, 1}) {
      WebGLRenderingContextBase context;
      context.gl.pack = {alignment, skips ? width + 2 : 0, skips, skips};
      DOMArrayBufferView pixels(2048);
      pixels.type_size = type == GL_FLOAT ? 4 : 1;
      for (int position : {0, -1, 7, std::numeric_limits<int>::min(), std::numeric_limits<int>::max()}) {
        if (mode == "pack" && position != 0) continue;
        std::fill(pixels.bytes.begin(), pixels.bytes.end(), 0xa5);
        auto expected = pixels.bytes;
        GL native;
        native.pack = context.gl.pack;
        native.ReadPixels(position, position, width, height, GL_RGBA, type,
                          expected.data() + offset * pixels.type_size);
        const int calls = context.gl.calls;
        context.ReadPixelsHelper(position, position, width, height, GL_RGBA, type, &pixels, offset);
        assert(context.gl.calls == calls + 1 && context.errors.empty() && context.gl.errors.empty());
        assert(pixels.bytes == expected);
        const auto first = pixels.bytes;
        context.ReadPixelsHelper(position, position, width, height, GL_RGBA, type, &pixels, offset);
        assert(pixels.bytes == first);
      }
    }
  } else if (mode == "errors") {
    for (int test = 0; test < 10; ++test) {
      WebGLRenderingContextBase context;
      DOMArrayBufferView pixels;
      auto original = pixels.bytes;
      auto* dest = &pixels;
      int width = 3, height = 2;
      int64_t offset = 0;
      GLenum format = GL_RGBA, type = GL_UNSIGNED_BYTE;
      context.gl.errors.push_back(GL_INVALID_ENUM);
      switch (test) {
        case 0: dest = nullptr; break;
        case 1: offset = -1; break;
        case 2: offset = std::numeric_limits<int64_t>::max(); pixels.type_size = 4; break;
        case 3: offset = pixels.byteLength() + 1; break;
        case 4: width = -1; break;
        case 5: format = 999; break;
        case 6: pixels.bytes.resize(1); original = pixels.bytes; break;
        case 7: context.bound = true; context.framebuffer.complete = false; break;
        case 8: context.gl.fail = true; break;
        case 9: type = GL_FLOAT; break;
      }
      context.ReadPixelsHelper(0, 0, width, height, format, type, dest, offset);
      assert(pixels.bytes == original);
      assert(context.gl.errors.front() == GL_INVALID_ENUM);
      if (test == 8) {
        assert(context.gl.calls == 1 && context.errors.empty());
        assert(context.gl.errors.size() == 2 && context.gl.errors.back() == GL_INVALID_OPERATION);
      } else { assert(context.gl.calls == 0 && context.errors.size() == 1); }
    }
  } else if (mode == "lifecycle") {
    for (int test = 0; test < 6; ++test) {
      WebGLRenderingContextBase context;
      DOMArrayBufferView pixels(test >= 4 ? 0 : 512);
      const auto original = pixels.bytes;
      if (test == 0) context.lost = true;
      if (test == 1) context.lose_on_clear = true;
      if (test == 2) context.drawing = false;
      if (test == 3) context.drawing_buffer.bind_ok = false;
      context.ReadPixelsHelper(0, 0, test == 4 ? 0 : 3, test == 5 ? 0 : 2,
                               GL_RGBA, GL_UNSIGNED_BYTE, &pixels, 0);
      assert(context.errors.empty() && pixels.bytes == original);
      assert(context.gl.calls == (test >= 4 ? 1 : 0));
    }
  } else { assert(false); }
}
'''
