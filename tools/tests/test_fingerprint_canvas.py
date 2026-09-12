"""Bounded Canvas noise/copy regressions; no browser build or network access.

C++ tests execute extracted patch code with Skia ownership/readback stubs.
Optional local-source checks use CHROMIX_CANVAS_BASELINE_ROOT and
CHROMIX_CANVAS_SOURCE_ROOT; they never modify either input tree.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import subprocess

import pytest

from test_restored_patch_contexts import SOURCE_SECTIONS as RESTORED_SECTIONS

ROOT = Path(__file__).resolve().parents[2]
PATCH_BIN = shutil.which("gpatch") or shutil.which("patch")
CXX = os.environ.get("CXX") or shutil.which("clang++") or shutil.which("g++")
BASELINE = ROOT / ".chromix-build-verify/sparse-real110-81cwzua4/context-repair/linux/upstream"


def patch_path(number):
    paths = list((ROOT / "patches").glob(f"{number}-*.patch"))
    assert len(paths) == 1
    return paths[0]


def target_path(number):
    return re.search(r"^\+\+\+ b/(.*)$", patch_path(number).read_text(), re.M)[1]


def source_fixture(number):
    lines = []
    for first, text in SOURCE_SECTIONS[number]:
        assert len(lines) < first
        lines.extend("// unrelated source line\n" for _ in range(first - 1 - len(lines)))
        lines.extend(text.splitlines(keepends=True))
    return "".join(lines) + "// not EOF\n// trailing context\n"


def apply_patch(directory, number, reverse=False, offsets=False):
    if not PATCH_BIN:
        pytest.skip("GNU patch is required")
    command = [PATCH_BIN, "-p1", "--fuzz=0", "--batch", "--forward", "--get=0",
               "--no-backup-if-mismatch", "--reject-file=-", "-i", str(patch_path(number))]
    if reverse:
        command.append("--reverse")
    result = subprocess.run(command, cwd=directory, text=True, capture_output=True,
                            timeout=15, env={**os.environ, "LC_ALL": "C", "PATCH_GET": "0"})
    assert result.returncode == 0, result.stdout + result.stderr
    assert "fuzz" not in result.stdout
    if not offsets:
        assert "offset" not in result.stdout


@pytest.fixture(scope="module")
def patched_sources(tmp_path_factory):
    directory = tmp_path_factory.mktemp("canvas-sources")
    sources = {}
    for number in ("0020", "0031"):
        path = directory / target_path(number)
        path.parent.mkdir(parents=True, exist_ok=True)
        original = source_fixture(number)
        path.write_bytes(original.encode("utf-8"))
        apply_patch(directory, number)
        sources[number] = path.read_text()
        apply_patch(directory, number, reverse=True)
        assert path.read_text() == original
    return sources


@pytest.mark.parametrize("number", ["0020", "0031"])
def test_patch_applies_and_reverses(patched_sources, number):
    assert patched_sources[number] != source_fixture(number)


@pytest.mark.parametrize("number", ["0020", "0031"])
def test_fixed_baseline_apply_reverse_and_fixture_provenance(tmp_path, number):
    baseline = Path(os.environ.get("CHROMIX_CANVAS_BASELINE_ROOT", BASELINE))
    original_path = baseline / target_path(number)
    if not original_path.exists():
        pytest.skip("local pinned Chromium 152 baseline is not available")
    original = original_path.read_bytes()
    timestamp = original_path.stat().st_mtime_ns
    lines = original.decode().splitlines(keepends=True)
    for first, text in SOURCE_SECTIONS[number]:
        assert "".join(lines[first - 1:first - 1 + len(text.splitlines())]) == text
    path = tmp_path / target_path(number)
    path.parent.mkdir(parents=True)
    path.write_bytes(original)
    apply_patch(tmp_path, number)
    if number == "0020":
        apply_patch(tmp_path, "0076", offsets=True)
        apply_patch(tmp_path, "0076", reverse=True, offsets=True)
    apply_patch(tmp_path, number, reverse=True)
    assert path.read_bytes() == original
    assert (original_path.read_bytes(), original_path.stat().st_mtime_ns) == (original, timestamp)


def test_includes_and_bounded_scope(patched_sources):
    for number, source in patched_sources.items():
        assert '#include "base/strings/string_number_conversions.h"' in source
        assert '#include "base/uxr_config.h"' in source
        assert '#include "ui/gfx/skia_span_util.h"' in source
        additions = "\n".join(line[1:] for line in patch_path(number).read_text().splitlines()
                              if line.startswith("+") and not line.startswith("+++"))
        assert "#pragma" not in additions
        assert "UNSAFE_BUFFERS" not in additions
        assert "base/command_line.h" not in additions
    assert patched_sources["0031"].count("UxrCopyAndNoiseEncodeBuffer(pixmap_, retained_image_)") == 2
    series = [line.strip() for line in (ROOT / "patches/series").read_text().splitlines()
              if line.strip() and not line.lstrip().startswith("#")]
    assert [Path(line).name[:4] for line in series] == [f"{i:04d}" for i in range(1, 127)]
    for number in ("0020", "0031"):
        assert series[int(number) - 1] == patch_path(number).relative_to(ROOT).as_posix()


def test_real_header_api_syntax(tmp_path, patched_sources):
    source_root = Path(os.environ.get("CHROMIX_CANVAS_SOURCE_ROOT", ROOT / ".chromix-build-verify/src"))
    compiler = source_root / "third_party/llvm-build/Release+Asserts/bin/clang++"
    generated = source_root / "out/Chromix/gen"
    libcxx = source_root / "third_party/libc++/src/include"
    if not compiler.exists() or not generated.exists() or not libcxx.exists():
        pytest.skip("local Chromium clang, libc++ and generated headers are required")
    source20, source31 = patched_sources["0020"], patched_sources["0031"]
    start = source20.index("  const auto& ph_config =")
    noise = source20[start:source20.index("\n  return image_data;", start)]
    start = source31.index("namespace {")
    helper = source31[start:source31.index("}  // namespace", start) + len("}  // namespace")]
    unit = tmp_path / "headers.cc"
    unit.write_text('''#include "base/strings/string_number_conversions.h"
#include "base/uxr_config.h"
#include "third_party/skia/include/core/SkImage.h"
#include "ui/gfx/skia_span_util.h"
struct ImageData { SkPixmap GetSkPixmap() const; };
void ReadNoise(ImageData* image_data, int sx, int sy) {
''' + noise + "}\n" + helper + '''
bool Encode(SkPixmap& pm, sk_sp<SkImage>& image) {
  return UxrCopyAndNoiseEncodeBuffer(pm, image);
}
''')
    includes = [source_root, generated, source_root / "third_party/skia",
                source_root / "base/allocator/partition_allocator/src",
                generated / "base/allocator/partition_allocator/src"]
    command = [str(compiler), "-std=c++23", "-fsyntax-only", "-Wall", "-Werror",
               "-D_LIBCPP_HARDENING_MODE=_LIBCPP_HARDENING_MODE_EXTENSIVE",
               "-nostdinc++", "-isystem", str(libcxx),
               "-isystem", str(source_root / "buildtools/third_party/libc++")]
    command.extend(f"-I{path}" for path in includes)
    result = subprocess.run(command + [str(unit)], text=True, capture_output=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.fixture(scope="module")
def runtime_binary(tmp_path_factory, patched_sources):
    if not CXX:
        pytest.skip("a C++20 compiler is required")
    source20 = patched_sources["0020"]
    start = source20.index("  const auto& ph_config =")
    noise = source20[start:source20.index("\n  return image_data;", start)]
    source31 = patched_sources["0031"]
    start = source31.index("namespace blink {")
    constructors = source31[start:source31.index("// not EOF", start)]
    directory = tmp_path_factory.mktemp("canvas-runtime")
    source = directory / "canvas.cc"
    source.write_text(CPP_SUPPORT + "\nvoid ReadNoise(ImageData* image_data, int sx, int sy) {\n" +
                      noise + "}\n" + constructors + "\n}  // namespace blink\n" + CPP_TESTS)
    binary = directory / "canvas"
    result = subprocess.run([CXX, "-std=c++20", "-O1", "-g", "-Wall", "-Wextra", "-Werror",
                             "-fsanitize=address,undefined", "-fno-sanitize-recover=all",
                             str(source), "-o", str(binary)], text=True, capture_output=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    return binary


@pytest.mark.parametrize("case", ["transparent-oob", "coordinates", "channels", "native", "padding",
                                  "constructors", "lifetime", "failures", "repeat", "uint64-seeds",
                                  "uint64-high-bits", "legacy-seeds", "default-native"])
def test_extracted_noise_and_copy(runtime_binary, case):
    result = subprocess.run([str(runtime_binary), case], text=True, capture_output=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr


SOURCE_SECTIONS = {
    "0031": RESTORED_SECTIONS["0031"],
    "0020": [
        (1, '''// Copyright 2016 The Chromium Authors
// Use of this source code is governed by a BSD-style license that can be
// found in the LICENSE file.

#include "third_party/blink/renderer/modules/canvas/canvas2d/base_rendering_context_2d.h"

#include <cmath>
#include <cstdint>
#include <cstdlib>
#include <limits>
#include <memory>
#include <optional>
#include <utility>

#include "base/check.h"
#include "base/check_op.h"
#include "base/location.h"
#include "base/memory/scoped_refptr.h"
#include "base/memory/weak_ptr.h"
#include "base/metrics/histogram_functions.h"
#include "base/notreached.h"
#include "base/numerics/checked_math.h"
#include "base/numerics/safe_conversions.h"
#include "base/task/single_thread_task_runner.h"
#include "base/time/time.h"
#include "cc/paint/paint_canvas.h"
'''),
        (102, '''#include "third_party/blink/renderer/platform/wtf/casting.h"
#include "third_party/blink/renderer/platform/wtf/forward.h"
#include "third_party/blink/renderer/platform/wtf/math_extras.h"
#include "third_party/blink/renderer/platform/wtf/text/wtf_string.h"
#include "ui/gfx/geometry/skia_conversions.h"
#include "ui/gfx/geometry/vector2d_f.h"

// Including "base/time/time.h" triggers a bug in IWYU.
// https://github.com/include-what-you-use/include-what-you-use/issues/1122
'''),
        (519, '''    }
    if (read_pixels_successful && RuntimeEnabledFeatures::FingerprintingCanvasImageDataNoiseEnabled()) {
      StaticBitmapImage::ShuffleSubchannelColorData(image_data_pixmap.addr(), image_data_pixmap.info(), sx, sy);
    }
  }

  return image_data;
}

void BaseRenderingContext2D::putImageData(ImageData* data,
'''),
    ],
}

CPP_SUPPORT = r'''
#include <algorithm>
#include <cassert>
#include <charconv>
#include <climits>
#include <cstdint>
#include <cstring>
#include <limits>
#include <memory>
#include <span>
#include <string>
#include <utility>
#include <vector>
#define MSAN_CHECK_MEM_IS_INITIALIZED(p, n) ((void)0)
template<class T> using sk_sp = std::shared_ptr<T>;
template<class T> using scoped_refptr = std::shared_ptr<T>;
namespace base {
struct UxrConfig {
  std::string seed = "12345";
  bool disabled = false;
  bool synthetic = true;
  static UxrConfig& GetInstance() { static UxrConfig config; return config; }
  bool Has(const char* key) const { assert(std::string(key) == "uxr-disable-fingerprint-noise"); return disabled; }
  std::string Get(const char* key) const {
    if (std::string(key) == "uxr-synthetic-device-tests") return synthetic ? "true" : "";
    assert(std::string(key) == "uxr-canvas-seed"); return seed;
  }
};
bool StringToUint64(const std::string& text, uint64_t* value) {
  const char* start = text.data();
  if (!text.empty() && text.front() == '+') ++start;
  auto result = std::from_chars(start, text.data() + text.size(), *value);
  return result.ec == std::errc() && result.ptr == text.data() + text.size();
}
template<class T> std::unique_ptr<T> WrapUnique(T* ptr) { return std::unique_ptr<T>(ptr); }
}
enum SkColorType { kRGBA_8888_SkColorType, kBGRA_8888_SkColorType, kRGBA_F16_SkColorType };
constexpr SkColorType kN32_SkColorType = kBGRA_8888_SkColorType;
enum SkAlphaType { kUnpremul_SkAlphaType, kPremul_SkAlphaType, kOpaque_SkAlphaType };
struct SkImageInfo {
  int w = 0, h = 0;
  SkColorType ct = kRGBA_8888_SkColorType;
  SkAlphaType at = kUnpremul_SkAlphaType;
  static SkImageInfo Make(int w, int h, SkColorType ct, SkAlphaType at, int) { return {w, h, ct, at}; }
  int width() const { return w; }
  int height() const { return h; }
  SkColorType colorType() const { return ct; }
  SkAlphaType alphaType() const { return at; }
  int refColorSpace() const { return 0; }
  bool isEmpty() const { return w <= 0 || h <= 0; }
  SkImageInfo makeAlphaType(SkAlphaType alpha) const { auto info = *this; info.at = alpha; return info; }
  bool validRowBytes(size_t rb) const {
    const size_t bpp = ct == kRGBA_F16_SkColorType ? 8 : 4;
    return rb >= minRowBytes() && rb % bpp == 0;
  }
  size_t minRowBytes() const { return size_t(w) * (ct == kRGBA_F16_SkColorType ? 8 : 4); }
  size_t computeByteSize(size_t rb) const {
    if (isEmpty()) return 0;
    size_t last = minRowBytes();
    if (rb < last || (h > 1 && rb > (SIZE_MAX - last) / size_t(h - 1))) return SIZE_MAX;
    return size_t(h - 1) * rb + last;
  }
  static bool ByteSizeOverflowed(size_t n) { return n == SIZE_MAX; }
};
struct SkPixmap {
  SkImageInfo info_;
  const void* address = nullptr;
  size_t rb = 0;
  SkPixmap() = default;
  SkPixmap(SkImageInfo info, const void* addr, size_t row) : info_(info), address(addr), rb(row) {}
  const SkImageInfo& info() const { return info_; }
  int width() const { return info_.w; }
  int height() const { return info_.h; }
  SkColorType colorType() const { return info_.ct; }
  size_t rowBytes() const { return rb; }
  size_t computeByteSize() const { return info_.computeByteSize(rb); }
  const void* addr() const { return address; }
  void* writable_addr() const { return const_cast<void*>(address); }
  void reset() { *this = {}; }
  bool readPixels(const SkImageInfo& info, void* dst, size_t row) const {
    for (int y = 0; y < info.h; ++y)
      std::memcpy(static_cast<uint8_t*>(dst) + size_t(y) * row,
                  static_cast<const uint8_t*>(addr()) + size_t(y) * rowBytes(),
                  info.minRowBytes());
    return true;
  }
};
int allocation_count = 0, fail_allocation = 0, raster_count = 0, fail_raster = 0;
struct SkData {
  std::vector<uint8_t> bytes;
  explicit SkData(size_t n) : bytes(n, 0) {}
  static sk_sp<SkData> MakeUninitialized(size_t n) {
    if (++allocation_count == fail_allocation) return nullptr;
    return std::make_shared<SkData>(n);
  }
  void* writable_data() { return bytes.data(); }
};
struct SkImage {
  SkPixmap pixels;
  sk_sp<SkData> storage;
  bool peek_ok = true;
  bool peekPixels(SkPixmap* pm) const { if (!peek_ok) return false; *pm = pixels; return true; }
};
namespace SkImages {
sk_sp<SkImage> RasterFromData(const SkImageInfo& info, sk_sp<SkData> data, size_t rb) {
  if (++raster_count == fail_raster) return nullptr;
  auto image = std::make_shared<SkImage>();
  image->pixels = {info, data->writable_data(), rb};
  image->storage = std::move(data);
  return image;
}
}
namespace gfx {
struct Size {
  int w, h;
  Size(int width, int height) : w(width), h(height) {}
  bool IsEmpty() const { return w <= 0 || h <= 0; }
};
std::span<const uint8_t> SkPixmapToSpan(const SkPixmap& pm) {
  return {static_cast<const uint8_t*>(pm.addr()), pm.computeByteSize()};
}
std::span<uint8_t> SkPixmapToWritableSpan(const SkPixmap& pm) {
  if (SkImageInfo::ByteSizeOverflowed(pm.computeByteSize())) return {};
  return {static_cast<uint8_t*>(pm.writable_addr()), pm.computeByteSize()};
}
sk_sp<SkData> MakeSkDataFromSpanWithCopy(std::span<const uint8_t> bytes) {
  auto data = SkData::MakeUninitialized(bytes.size());
  if (data) std::copy(bytes.begin(), bytes.end(), data->bytes.begin());
  return data;
}
}
struct PaintImage {
  sk_sp<SkImage> image;
  bool texture = false, lazy = false, worklet = false, read_ok = true;
  explicit operator bool() const { return bool(image); }
  bool IsPaintWorklet() const { return worklet; }
  bool IsTextureBacked() const { return texture; }
  bool IsLazyGenerated() const { return lazy; }
  SkImageInfo GetSkImageInfo() const { return image->pixels.info(); }
  SkColorType GetColorType() const { return image->pixels.colorType(); }
  sk_sp<SkImage> GetSwSkImage() const { return image; }
  bool readPixels(const SkImageInfo& info, void* dst, size_t rb, int, int) const {
    if (!read_ok) return false;
    for (int y = 0; y < info.h; ++y)
      std::memcpy(static_cast<uint8_t*>(dst) + size_t(y) * rb,
                  static_cast<const uint8_t*>(image->pixels.addr()) + size_t(y) * image->pixels.rowBytes(),
                  info.minRowBytes());
    return true;
  }
};
struct StaticBitmapImage {
  PaintImage paint;
  PaintImage PaintImageForCurrentFrame() const { return paint; }
};
struct ImageData { SkPixmap pm; SkPixmap GetSkPixmap() const { return pm; } };
namespace blink {
class ImageDataBuffer {
 public:
  static std::unique_ptr<ImageDataBuffer> Create(scoped_refptr<StaticBitmapImage>);
  static std::unique_ptr<ImageDataBuffer> Create(const SkPixmap&);
  bool IsValid() const { return is_valid_; }
  sk_sp<SkImage> retained_image_;
  SkPixmap pixmap_;
 private:
  ImageDataBuffer(scoped_refptr<StaticBitmapImage>);
  ImageDataBuffer(const SkPixmap&);
  bool is_valid_ = false;
};
}
'''

CPP_TESTS = r'''
using blink::ImageDataBuffer;
struct Fixture {
  SkImageInfo info;
  size_t rb;
  std::vector<uint8_t> bytes;
  Fixture(SkColorType ct = kRGBA_8888_SkColorType, int w = 7, int h = 4, size_t padding = 12)
      : info{w, h, ct, kUnpremul_SkAlphaType}, rb(info.minRowBytes() + padding), bytes(rb * size_t(h), 0xa7) {
    for (int y = 0; y < h; ++y) for (int x = 0; x < w; ++x) {
      const size_t p = size_t(y) * rb + size_t(x) * (ct == kRGBA_F16_SkColorType ? 8 : 4);
      bytes[p] = uint8_t(40 + x); bytes[p + 1] = uint8_t(90 + y); bytes[p + 2] = uint8_t(150 + x);
      bytes[p + 3] = uint8_t(x == 0 ? 0 : x == 1 ? 1 : 255);
    }
  }
  SkPixmap pm() { return {info, bytes.data(), rb}; }
  std::shared_ptr<StaticBitmapImage> image(int mode) {
    auto result = std::make_shared<StaticBitmapImage>();
    result->paint.image = std::make_shared<SkImage>();
    result->paint.image->pixels = pm();
    result->paint.texture = mode == 1;
    result->paint.lazy = mode == 2;
    if (mode == 3) result->paint.image->pixels.info_.at = kPremul_SkAlphaType;
    return result;
  }
};
std::vector<uint8_t> Bytes(const SkPixmap& pm) {
  auto span = gfx::SkPixmapToSpan(pm); return {span.begin(), span.end()};
}
uint8_t Expected(uint8_t v, uint32_t seed, uint32_t x, uint32_t y, uint32_t ch) {
  uint32_t z = seed ^ (x * 374761393u) ^ (y * 668265263u) ^ (ch * 0x9e3779b9u) ^ (v * 2654435761u);
  z = (z ^ (z >> 16)) * 0x85ebca6bu;
  z = (z ^ (z >> 13)) * 0xc2b2ae35u;
  z ^= z >> 16;
  return uint8_t(std::clamp(int(v) + ((z & 1) ? 1 : -1), 0, 255));
}
void CheckPixels(Fixture& f, const SkPixmap& result, uint32_t sx = 0, uint32_t sy = 0,
                 uint32_t seed = 12345) {
  auto actual = gfx::SkPixmapToSpan(result);
  for (int y = 0; y < f.info.h; ++y) for (int x = 0; x < f.info.w; ++x) {
    size_t a = size_t(y) * result.rowBytes() + size_t(x) * 4;
    size_t b = size_t(y) * f.rb + size_t(x) * 4;
    assert(actual[a + 3] == f.bytes[b + 3]);
    for (uint32_t c = 0; c < 3; ++c) {
      size_t i = f.info.ct == kBGRA_8888_SkColorType ? 2 - c : c;
      uint8_t expected = f.bytes[b + 3] == 0 ? f.bytes[b + i] : Expected(f.bytes[b + i], seed, sx + uint32_t(x), sy + uint32_t(y), c);
      assert(actual[a + i] == expected);
    }
  }
}
int main(int argc, char** argv) {
  assert(argc == 2);
  const std::string test = argv[1];
  if (test == "transparent-oob") {
    Fixture f;
    for (int y = 0; y < f.info.h; ++y) std::fill_n(f.bytes.begin() + size_t(y) * f.rb, 4, 0);
    auto source = f.bytes;
    ImageData data{f.pm()}; ReadNoise(&data, -1, -1);
    for (int y = 0; y < f.info.h; ++y) for (int i = 0; i < 4; ++i) assert(f.bytes[size_t(y) * f.rb + i] == 0);
    f.bytes = source;
    auto encoded = ImageDataBuffer::Create(f.pm()); assert(encoded); CheckPixels(f, encoded->pixmap_);
    assert(f.bytes == source);
    std::fill(f.bytes.begin(), f.bytes.end(), 0);
    data = {f.pm()}; ReadNoise(&data, INT_MAX, INT_MIN);
    assert(std::all_of(f.bytes.begin(), f.bytes.end(), [](auto v) { return v == 0; }));
  } else if (test == "coordinates") {
    for (int sx : {INT_MIN, -3, 0, INT_MAX}) for (int sy : {INT_MIN, -2, 0, INT_MAX}) {
      Fixture original, output; ImageData data{output.pm()}; ReadNoise(&data, sx, sy);
      CheckPixels(original, output.pm(), uint32_t(sx), uint32_t(sy));
    }
    Fixture large(kRGBA_8888_SkColorType, 12, 8), crop(kRGBA_8888_SkColorType, 4, 3);
    for (int y = 0; y < 3; ++y) std::copy_n(large.bytes.begin() + size_t(y + 2) * large.rb + 12, 16, crop.bytes.begin() + size_t(y) * crop.rb);
    ImageData a{large.pm()}, b{crop.pm()}; ReadNoise(&a, 0, 0); ReadNoise(&b, 3, 2);
    for (int y = 0; y < 3; ++y) assert(std::equal(crop.bytes.begin() + size_t(y) * crop.rb, crop.bytes.begin() + size_t(y) * crop.rb + 16, large.bytes.begin() + size_t(y + 2) * large.rb + 12));
  } else if (test == "channels") {
    Fixture rgba, bgra(kBGRA_8888_SkColorType);
    for (int y = 0; y < rgba.info.h; ++y) for (int x = 0; x < rgba.info.w; ++x) {
      size_t p = size_t(y) * rgba.rb + size_t(x) * 4;
      if (x == 2) { rgba.bytes[p] = 0; rgba.bytes[p + 2] = 255; }
      std::copy_n(rgba.bytes.begin() + p, 4, bgra.bytes.begin() + p);
      std::swap(bgra.bytes[p], bgra.bytes[p + 2]);
    }
    auto a = ImageDataBuffer::Create(rgba.pm()), b = ImageDataBuffer::Create(bgra.pm());
    CheckPixels(rgba, a->pixmap_); CheckPixels(bgra, b->pixmap_);
    ImageData ra{rgba.pm()}, rb{bgra.pm()}; ReadNoise(&ra, 0, 0); ReadNoise(&rb, 0, 0);
    assert(Bytes(a->pixmap_) == Bytes(rgba.pm())); assert(Bytes(b->pixmap_) == Bytes(bgra.pm()));
    for (int y = 0; y < rgba.info.h; ++y) for (int x = 0; x < rgba.info.w; ++x) {
      size_t p = size_t(y) * rgba.rb + size_t(x) * 4;
      assert(rgba.bytes[p] == bgra.bytes[p + 2]); assert(rgba.bytes[p + 2] == bgra.bytes[p]);
    }
  } else if (test == "uint64-seeds") {
    const std::pair<uint64_t, uint32_t> vectors[] = {
        {1, 1}, {12345, 12345}, {UINT32_MAX, UINT32_MAX},
        {uint64_t{1} << 32, 0x469913f8u}, {uint64_t{1} << 63, 0x6448276au},
        {UINT64_MAX, 0x9c0ff28bu}};
    for (auto [seed64, folded] : vectors) {
      auto& config = base::UxrConfig::GetInstance(); config.seed = std::to_string(seed64);
      for (auto ct : {kRGBA_8888_SkColorType, kBGRA_8888_SkColorType}) {
        Fixture original(ct, 12, 8), output = original;
        auto before = original.bytes;
        ImageData data{output.pm()}; ReadNoise(&data, 0, 0);
        CheckPixels(original, output.pm(), 0, 0, folded);
        assert(output.bytes != before);
        for (int mode = -1; mode < 4; ++mode) {
          auto encoded = mode < 0 ? ImageDataBuffer::Create(original.pm()) : ImageDataBuffer::Create(original.image(mode));
          assert(encoded); CheckPixels(original, encoded->pixmap_, 0, 0, folded);
          assert(original.bytes == before);
        }
        for (int sx : {INT_MIN, -3, 0, INT_MAX}) for (int sy : {INT_MIN, -2, 0, INT_MAX}) {
          Fixture shifted = original; ImageData read{shifted.pm()}; ReadNoise(&read, sx, sy);
          CheckPixels(original, shifted.pm(), uint32_t(sx), uint32_t(sy), folded);
        }
        Fixture crop(ct, 4, 3);
        for (int y = 0; y < 3; ++y)
          std::copy_n(original.bytes.begin() + size_t(y + 2) * original.rb + 12, 16, crop.bytes.begin() + size_t(y) * crop.rb);
        ImageData read_crop{crop.pm()}; ReadNoise(&read_crop, 3, 2);
        auto encoded = ImageDataBuffer::Create(original.pm()); assert(encoded);
        auto pixels = gfx::SkPixmapToSpan(encoded->pixmap_);
        for (int y = 0; y < 3; ++y)
          assert(std::equal(crop.bytes.begin() + size_t(y) * crop.rb, crop.bytes.begin() + size_t(y) * crop.rb + 16,
                            pixels.begin() + size_t(y + 2) * encoded->pixmap_.rowBytes() + 12));
      }
      Fixture rgba, bgra(kBGRA_8888_SkColorType);
      for (int y = 0; y < rgba.info.h; ++y) for (int x = 0; x < rgba.info.w; ++x) {
        size_t p = size_t(y) * bgra.rb + size_t(x) * 4;
        std::copy_n(rgba.bytes.begin() + p, 4, bgra.bytes.begin() + p);
        std::swap(bgra.bytes[p], bgra.bytes[p + 2]);
      }
      auto a = ImageDataBuffer::Create(rgba.pm()), b = ImageDataBuffer::Create(bgra.pm()); assert(a && b);
      auto av = Bytes(a->pixmap_), bv = Bytes(b->pixmap_);
      for (int y = 0; y < rgba.info.h; ++y) for (int x = 0; x < rgba.info.w; ++x) {
        size_t p = size_t(y) * rgba.rb + size_t(x) * 4;
        assert(av[p] == bv[p + 2] && av[p + 1] == bv[p + 1] && av[p + 2] == bv[p] && av[p + 3] == bv[p + 3]);
      }
      config.seed = "000" + std::to_string(seed64);
      auto leading_zero = ImageDataBuffer::Create(rgba.pm()); assert(leading_zero);
      assert(Bytes(leading_zero->pixmap_) == av);
      config.disabled = true;
      ImageData native{rgba.pm()}; auto before = rgba.bytes; ReadNoise(&native, 0, 0);
      auto disabled = ImageDataBuffer::Create(rgba.pm()); assert(disabled);
      assert(Bytes(disabled->pixmap_) == Bytes(rgba.pm()) && rgba.bytes == before);
      config.disabled = false;
    }
  } else if (test == "uint64-high-bits") {
    Fixture f(kRGBA_8888_SkColorType, 12, 8); auto before = f.bytes;
    auto& config = base::UxrConfig::GetInstance(); config.seed = "12345";
    auto old = ImageDataBuffer::Create(f.pm()); assert(old);
    std::vector<std::vector<uint8_t>> signatures{Bytes(old->pixmap_)};
    for (int bit = 32; bit < 64; ++bit) {
      for (uint64_t low : {uint64_t{0}, uint64_t{12345}}) {
        config.seed = std::to_string((uint64_t{1} << bit) | low);
        auto encoded = ImageDataBuffer::Create(f.pm()); assert(encoded);
        auto current = Bytes(encoded->pixmap_);
        assert(current != Bytes(f.pm()));
        for (const auto& prior : signatures) assert(current != prior);
        signatures.push_back(current);
        Fixture read = f; ImageData data{read.pm()}; ReadNoise(&data, 0, 0);
        assert(Bytes(read.pm()) == current && f.bytes == before);
      }
    }
  } else if (test == "legacy-seeds") {
    std::vector<uint32_t> seeds{1u, 12345u, 0x7fffffffu, 0x80000000u, UINT32_MAX};
    for (uint32_t i = 1; i <= 256; ++i) seeds.push_back(i * 2654435761u);
    for (auto seed : seeds) for (auto ct : {kRGBA_8888_SkColorType, kBGRA_8888_SkColorType}) {
      base::UxrConfig::GetInstance().seed = std::to_string(seed);
      Fixture original(ct), read = original;
      ImageData data{read.pm()}; ReadNoise(&data, 0, 0);
      CheckPixels(original, read.pm(), 0, 0, seed);
      auto encoded = ImageDataBuffer::Create(original.pm()); assert(encoded);
      CheckPixels(original, encoded->pixmap_, 0, 0, seed);
      assert(Bytes(read.pm()) == Bytes(encoded->pixmap_));
    }
  } else if (test == "default-native") {
    auto& config = base::UxrConfig::GetInstance();
    config.synthetic = false;
    for (const std::string seed : {"1", "12345", "4294967296", "18446744073709551615"}) {
      config.seed = seed;
      Fixture f; auto before = f.bytes; ImageData data{f.pm()};
      ReadNoise(&data, 0, 0);
      auto encoded = ImageDataBuffer::Create(f.pm()); assert(encoded);
      assert(f.bytes == before && Bytes(encoded->pixmap_) == Bytes(f.pm()));
    }
  } else if (test == "native") {
    auto& config = base::UxrConfig::GetInstance();
    for (const std::string seed : {"", "0", "000", "invalid", "-1", "+1", " 1", "1 ",
                                    "1\n", "1x", "0x1", "18446744073709551616", "12345"}) {
      config.seed = seed; config.disabled = seed == "12345";
      Fixture f; auto before = f.bytes; ImageData data{f.pm()}; ReadNoise(&data, 0, 0); assert(f.bytes == before);
      for (int mode = -1; mode < 4; ++mode) {
        auto encoded = mode < 0 ? ImageDataBuffer::Create(f.pm()) : ImageDataBuffer::Create(f.image(mode));
        assert(encoded); if (mode < 1) assert(encoded->pixmap_.addr() == f.bytes.data());
        for (int y = 0; y < f.info.h; ++y) assert(std::equal(f.bytes.begin() + size_t(y) * f.rb, f.bytes.begin() + size_t(y) * f.rb + f.info.minRowBytes(), static_cast<const uint8_t*>(encoded->pixmap_.addr()) + size_t(y) * encoded->pixmap_.rowBytes()));
      }
    }
    config.seed = "12345"; config.disabled = false;
    Fixture f(kRGBA_F16_SkColorType, 7, 4, 16); auto before = f.bytes; ImageData data{f.pm()}; ReadNoise(&data, 0, 0);
    auto encoded = ImageDataBuffer::Create(f.pm()); assert(encoded->pixmap_.addr() == f.bytes.data()); assert(f.bytes == before);
  } else if (test == "padding") {
    Fixture f; auto before = f.bytes; auto encoded = ImageDataBuffer::Create(f.pm());
    assert(encoded->pixmap_.rowBytes() == f.rb); auto out = Bytes(encoded->pixmap_);
    for (int y = 0; y < f.info.h - 1; ++y) for (size_t x = f.info.minRowBytes(); x < f.rb; ++x) assert(out[size_t(y) * f.rb + x] == 0xa7);
    ImageData data{f.pm()}; ReadNoise(&data, 0, 0);
    for (int y = 0; y < f.info.h; ++y) for (size_t x = f.info.minRowBytes(); x < f.rb; ++x) assert(f.bytes[size_t(y) * f.rb + x] == before[size_t(y) * f.rb + x]);
  } else if (test == "constructors" || test == "repeat") {
    for (SkColorType ct : {kRGBA_8888_SkColorType, kBGRA_8888_SkColorType}) for (int mode = -1; mode < 4; ++mode) {
      Fixture f(ct); auto before = f.bytes; std::vector<uint8_t> prior;
      for (int i = 0; i < 8; ++i) {
        auto encoded = mode < 0 ? ImageDataBuffer::Create(f.pm()) : ImageDataBuffer::Create(f.image(mode));
        assert(encoded && encoded->retained_image_); assert(encoded->pixmap_.addr() != f.bytes.data());
        CheckPixels(f, encoded->pixmap_); assert(f.bytes == before);
        auto current = Bytes(encoded->pixmap_); if (i) assert(current == prior); prior = current;
      }
    }
  } else if (test == "lifetime") {
    for (int mode = -1; mode < 4; ++mode) {
      std::unique_ptr<ImageDataBuffer> encoded; std::vector<uint8_t> before;
      { Fixture f; encoded = mode < 0 ? ImageDataBuffer::Create(f.pm()) : ImageDataBuffer::Create(f.image(mode)); before = Bytes(encoded->pixmap_); }
      std::vector<uint8_t> churn(8192, 99); assert(churn.front() == 99);
      assert(Bytes(encoded->pixmap_) == before);
      assert(encoded->retained_image_->storage->bytes.data() == encoded->pixmap_.addr());
    }
  } else if (test == "failures") {
    for (int mode = -1; mode < 4; ++mode) {
      for (int nth = 1; nth <= (mode >= 1 ? 2 : 1); ++nth) {
        Fixture f; auto before = f.bytes; allocation_count = 0; fail_allocation = nth;
        auto encoded = mode < 0 ? ImageDataBuffer::Create(f.pm()) : ImageDataBuffer::Create(f.image(mode));
        assert(!encoded && f.bytes == before); fail_allocation = 0;
        raster_count = 0; fail_raster = nth;
        encoded = mode < 0 ? ImageDataBuffer::Create(f.pm()) : ImageDataBuffer::Create(f.image(mode));
        assert(!encoded && f.bytes == before); fail_raster = 0;
      }
    }
    Fixture f; auto image = f.image(1); image->paint.read_ok = false; assert(!ImageDataBuffer::Create(image));
    image = f.image(0); image->paint.image->peek_ok = false; assert(!ImageDataBuffer::Create(image));
    assert(!ImageDataBuffer::Create(SkPixmap{})); assert(!ImageDataBuffer::Create(scoped_refptr<StaticBitmapImage>{}));
    auto invalid = f.pm(); invalid.rb = SIZE_MAX; assert(!ImageDataBuffer::Create(invalid));
  } else return 2;
}
'''
