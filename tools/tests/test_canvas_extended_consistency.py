"""Canvas encode/readback contracts using extracted C++ and local source fixtures.

No browser downloads or Chromium builds. Async integration checks require the
existing sparse Chromium source named by CHROMIX_CANVAS_ASYNC_BASELINE_ROOT.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import shutil
import subprocess

import pytest

from test_fingerprint_canvas import (
    CPP_SUPPORT,
    CXX,
    ROOT,
    apply_patch,
    patch_path,
    patched_sources,
    target_path,
)

ASYNC_BASELINE = Path("/tmp/chromix-canvas-xtj3x5h3/base")
ASYNC_CC = "third_party/blink/renderer/core/html/canvas/canvas_async_blob_creator.cc"
ASYNC_H = ASYNC_CC.removesuffix(".cc") + ".h"
BUFFER_H = "third_party/blink/renderer/platform/graphics/image_data_buffer.h"
ENCODER_CC = "third_party/blink/renderer/platform/image-encoders/image_encoder.cc"
ENCODER_BASELINES = ROOT / ".chromix-build-verify/sparse-real110-81cwzua4/context-repair"
ENCODER_PREIMAGE_SHA256 = "53ab322e6fa9b08db101bc553ca51a7568643353d0a7ea6784827df7c5f2f146"
BUFFER_CC = "third_party/blink/renderer/platform/graphics/image_data_buffer.cc"
PATCH_BIN = shutil.which("gpatch") or shutil.which("patch")


def function(source, signature):
    start = source.index(signature)
    first = source.index("{", start)
    depth = 1
    end = first + 1
    while depth:
        depth += (source[end] == "{") - (source[end] == "}")
        end += 1
    return source[start:end]


def compile_cpp(directory, source):
    if not CXX:
        pytest.skip("a C++20 compiler is required")
    path = directory / "canvas.cc"
    binary = directory / "canvas"
    path.write_text(source)
    result = subprocess.run(
        [CXX, "-std=c++20", "-O1", "-g", "-Wall", "-Wextra", "-Werror",
         "-Wno-unused-parameter", "-fsanitize=address,undefined",
         "-fno-sanitize-recover=all", str(path), "-o", str(binary)],
        text=True, capture_output=True, timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return binary


def run_case(binary, case):
    result = subprocess.run([str(binary), case], text=True, capture_output=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr


def test_async_patches_are_in_series_and_well_formed(tmp_path):
    from tools.check_patches import Report, check_bodies

    series = [line.strip() for line in (ROOT / "patches/series").read_text().splitlines()
              if line.strip() and not line.lstrip().startswith("#")]
    assert len(series) == 128
    for number in ("0121", "0122", "0123", "0124"):
        patch = patch_path(number)
        assert series[int(number) - 1] == patch.relative_to(ROOT).as_posix()
        shutil.copyfile(patch, tmp_path / patch.name)
    report = Report()
    check_bodies(report, tmp_path, False)
    assert not report.failures


def test_existing_noise_is_not_stacked(patched_sources):
    source = patched_sources["0020"]
    guard = source[source.index("    const auto& noise_config ="):]
    guard = guard[:guard.index("StaticBitmapImage::ShuffleSubchannelColorData")]
    assert '!noise_config.Has("uxr-disable-fingerprint-noise") && !persona_noise' in guard
    assert 'base::StringToUint64(noise_seed_text, &noise_seed)' in guard
    assert 'noise_seed_text.find_first_not_of("0123456789") == std::string::npos' in guard
    assert "noise_seed != 0u" in guard
    assert "read_pixels_successful &&" in guard


def test_uint64_seed_parsing_and_fold_match(patched_sources):
    readback = patched_sources["0020"]
    encode = patched_sources["0031"]
    read_fold = readback[readback.index("    uint64_t high ="):readback.index("    const SkPixmap ph_pm")]
    encode_fold = encode[encode.index("  uint64_t high ="):encode.index("  const bool normalize_alpha")]
    normalize = lambda text: " ".join(text.replace("ph_seed", "seed").split())
    assert normalize(read_fold) == normalize(encode_fold)
    for source in (readback, encode):
        assert "base::StringToUint(" not in source
        assert 'find_first_not_of("0123456789") == std::string::npos' in source
        assert "base::StringToUint64(" in source
        assert "0xbf58476d1ce4e5b9ULL" in source
        assert "0x94d049bb133111ebULL" in source


@pytest.fixture(scope="module")
def async_sources(tmp_path_factory):
    root = Path(os.environ.get("CHROMIX_CANVAS_ASYNC_BASELINE_ROOT", ASYNC_BASELINE))
    if not all((root / path).exists() for path in (ASYNC_CC, ASYNC_H, BUFFER_H)):
        pytest.skip("local sparse CanvasAsyncBlobCreator Chromium 152 source is required")
    if not PATCH_BIN:
        pytest.skip("GNU patch is required")
    directory = tmp_path_factory.mktemp("canvas-async-source")
    before = {}
    for path in (ASYNC_CC, ASYNC_H, BUFFER_H):
        original = root / path
        data = original.read_bytes()
        before[path] = (hashlib.sha256(data).hexdigest(), original.stat().st_mtime_ns)
        target = directory / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    for number in ("0121", "0122", "0123"):
        result = subprocess.run(
            [PATCH_BIN, "-p1", "--batch", "--forward", "--fuzz=0", "--get=0",
             "--no-backup-if-mismatch", "--reject-file=-", "-i", str(patch_path(number))],
            cwd=directory, text=True, capture_output=True, timeout=15,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "fuzz" not in result.stdout and "offset" not in result.stdout
    sources = {path: (directory / path).read_text() for path in (ASYNC_CC, ASYNC_H, BUFFER_H)}
    for number in ("0123", "0122", "0121"):
        result = subprocess.run(
            [PATCH_BIN, "-p1", "--batch", "--reverse", "--fuzz=0", "--get=0",
             "--no-backup-if-mismatch", "--reject-file=-", "-i", str(patch_path(number))],
            cwd=directory, text=True, capture_output=True, timeout=15,
        )
        assert result.returncode == 0, result.stdout + result.stderr
    for path, expected in before.items():
        original = root / path
        assert (hashlib.sha256(original.read_bytes()).hexdigest(), original.stat().st_mtime_ns) == expected
        assert (directory / path).read_bytes() == original.read_bytes()
    return sources


def test_async_ownership_and_single_preparation(async_sources):
    source = async_sources[ASYNC_CC]
    constructor = source[source.index("    : fail_encoder_initialization_for_test_"):source.index("CanvasAsyncBlobCreator::~")]
    assert constructor.count("ImageDataBuffer::Create(src_data_)") == 1
    assert constructor.index("ImageEncoder::MaxDimension") < constructor.index("ImageDataBuffer::Create")
    assert constructor.index("ImageDataBuffer::Create") < constructor.index("static_bitmap_image_loaded_ = true")
    schedule = function(source, "void CanvasAsyncBlobCreator::ScheduleAsyncBlobCreation(")
    assert "ImageDataBuffer::Create" not in schedule
    assert schedule.count("std::move(image_data_buffer_)") == 2
    dispose = function(source, "void CanvasAsyncBlobCreator::Dispose()")
    assert dispose.index("encoder_.reset()") < dispose.index("image_data_buffer_.reset()")
    assert "std::unique_ptr<ImageDataBuffer> image_data_buffer_;" in async_sources[ASYNC_H]
    assert "const SkPixmap& GetPixmap() const { return pixmap_; }" in async_sources[BUFFER_H]


def test_canvas_security_checks_remain_before_readback(tmp_path):
    root = Path(os.environ.get("CHROMIX_CANVAS_SOURCE_ROOT", ROOT / ".chromix-build-verify/src"))
    files = {
        "html": "third_party/blink/renderer/core/html/canvas/html_canvas_element.cc",
        "offscreen": "third_party/blink/renderer/core/offscreencanvas/offscreen_canvas.cc",
    }
    if not all((root / path).exists() for path in files.values()):
        pytest.skip("local Chromium Canvas API source is required")
    html = (root / files["html"]).read_text()
    offscreen = (root / files["offscreen"]).read_text()
    blob = function(html, "void HTMLCanvasElement::toBlob(")
    assert blob.index("!OriginClean()") < blob.index("Snapshot(kBackBuffer)")
    assert blob.index("ContextHasOpenLayers") < blob.index("Snapshot(kBackBuffer)")
    convert = function(offscreen, "ScriptPromise<Blob> OffscreenCanvas::convertToBlob(")
    for check in ("is_neutered_", "ContextHasOpenLayers", "!OriginClean()", "!IsPaintable()"):
        assert convert.index(check) < convert.index("context_->GetImage()")
    baseline = Path(os.environ.get("CHROMIX_CANVAS_BASELINE_ROOT", ROOT / ".chromix-build-verify/sparse-real110-81cwzua4/context-repair/linux/upstream"))
    path = baseline / target_path("0020")
    if not path.exists():
        pytest.skip("pre-Chromix Canvas2D baseline is required")
    target = tmp_path / target_path("0020")
    target.parent.mkdir(parents=True)
    original = path.read_bytes()
    target.write_bytes(original)
    apply_patch(tmp_path, "0020")
    apply_patch(tmp_path, "0076", offsets=True)
    read = function(target.read_text(), "ImageData* BaseRenderingContext2D::getImageDataInternal(")
    for check in ("!OriginClean()", "layer_count_ != 0", "base::CheckAdd(sx, sw)", "exception_state.HadException()"):
        assert read.index(check) < read.index("GetImageDataCacheFirst") < read.index("const auto& ph_config")
    assert path.read_bytes() == original


def support():
    source = CPP_SUPPORT.replace(
        "  SkAlphaType at = kUnpremul_SkAlphaType;",
        "  SkAlphaType at = kUnpremul_SkAlphaType;\n  int cs = 0;",
    ).replace(
        "SkAlphaType at, int) { return {w, h, ct, at}; }",
        "SkAlphaType at, int cs) { return {w, h, ct, at, cs}; }",
    ).replace("int refColorSpace() const { return 0; }", "int refColorSpace() const { return cs; }")
    source = source.replace("  bool isEmpty() const", "  SkImageInfo makeWH(int x, int y) const { auto i = *this; i.w = x; i.h = y; return i; }\n  bool isEmpty() const")
    source = source.replace("struct SkPixmap {", "bool fail_conversion = false;\nint conversions = 0;\nstruct SkPixmap {")
    old = function(source, "  bool readPixels(const SkImageInfo& info, void* dst, size_t row) const")
    source = source.replace(old, PIXMAP_CONVERSION)
    source = source.replace("  void reset() { *this = {}; }", "  void reset() { *this = {}; }\n  void reset(SkImageInfo i, const void* p, size_t row) { *this = {i, p, row}; }")
    source = source.replace("  bool peek_ok = true;", "  bool peek_ok = true;\n  bool isTextureBacked() const { return false; }\n  bool isLazyGenerated() const { return false; }\n  bool readPixels(SkImageInfo, void*, size_t, int, int) { return true; }")
    source = source.replace("  int width() const { return w; }", "  static SkImageInfo MakeN32Premul(int w, int h) { return {w, h, kN32_SkColorType, kPremul_SkAlphaType}; }\n  int bytesPerPixel() const { return ct == kRGBA_F16_SkColorType ? 8 : 4; }\n  int width() const { return w; }")
    old = function(source, "  bool readPixels(const SkImageInfo& info, void* dst, size_t rb, int, int) const")
    source = source.replace(old, "  bool readPixels(const SkImageInfo& info, void* dst, size_t rb, int, int) const { return read_ok && image->pixels.readPixels(info, dst, rb); }")
    source = source.replace("struct StaticBitmapImage {", "struct StaticBitmapImage : std::enable_shared_from_this<StaticBitmapImage> {")
    source = source.replace("  PaintImage paint;", "  PaintImage paint;\n  scoped_refptr<StaticBitmapImage> MakeUnaccelerated() { return shared_from_this(); }")
    source = source.replace("  bool IsValid() const", "  const SkPixmap& GetPixmap() const { return pixmap_; }\n  bool IsValid() const")
    return source + '''
bool legacy_noise = true, no_idle = false;
struct RuntimeEnabledFeatures {
  static bool FingerprintingCanvasImageDataNoiseEnabled() { return legacy_noise; }
  static bool NoIdleEncodingForWebTestsEnabled() { return no_idle; }
};
'''


def read_noise(patched_sources):
    source = patched_sources["0020"]
    guard_start = source.index("    const auto& noise_config =")
    guard_end = source.index("      StaticBitmapImage::ShuffleSubchannelColorData", guard_start)
    guard = source[guard_start:guard_end] + "      return true;\n    }\n  return false;\n}\n"
    legacy = "bool LegacyNoiseAllowed(bool read_pixels_successful) {\n" + guard
    start = source.index("  const auto& ph_config =")
    end = source.index("\n  return image_data;", start)
    return ("void ReadNoise(ImageData* image_data, int sx, int sy) {\n" +
            source[start:end] + "}\n" + legacy)


def buffer_code(patched_sources):
    source = patched_sources["0031"]
    return source[source.index("namespace blink {"):source.index("// not EOF")] + "\n}  // namespace blink\n"


@pytest.fixture(scope="module")
def extended_binary(tmp_path_factory, patched_sources):
    directory = tmp_path_factory.mktemp("canvas-extended-runtime")
    return compile_cpp(directory, support() + read_noise(patched_sources) +
                       buffer_code(patched_sources) + PIXEL_TESTS)


@pytest.mark.parametrize("case", ["premul", "opaque", "f16", "color-space", "invalid-layout", "conversion-failure", "noise-gate"])
def test_extended_pixel_contracts(extended_binary, case):
    run_case(extended_binary, case)


@pytest.fixture(scope="module")
def async_binary(tmp_path_factory, patched_sources, async_sources):
    source = async_sources[ASYNC_CC]
    first = source.index("CanvasAsyncBlobCreator::CanvasAsyncBlobCreator(")
    second = source.index("CanvasAsyncBlobCreator::CanvasAsyncBlobCreator(", first + 1)
    constructor = function(source[second:], "CanvasAsyncBlobCreator::CanvasAsyncBlobCreator(")
    names = ("Dispose", "ScheduleAsyncBlobCreation", "InitiateEncoding", "IdleEncodeRows",
             "ForceEncodeRows", "IdleTaskStartTimeoutEvent", "IdleTaskCompleteTimeoutEvent")
    methods = "\n".join(function(source, f"void CanvasAsyncBlobCreator::{name}(") for name in names)
    methods += "\n" + function(source, "bool CanvasAsyncBlobCreator::InitializeEncoder(")
    directory = tmp_path_factory.mktemp("canvas-async-runtime")
    return compile_cpp(directory, support() + read_noise(patched_sources) +
                       buffer_code(patched_sources) + ASYNC_SUPPORT + "namespace blink {\n" +
                       constructor + "\n" + methods + "\n}\n" + ASYNC_TESTS)


@pytest.mark.parametrize("case", ["idle-png", "idle-jpeg", "worker", "encoder-pool", "no-idle",
                                  "start-timeout", "complete-timeout", "allocation-failure",
                                  "init-failure", "start-init-failure", "row-failure", "snapshot-lifetime"])
def test_async_extracted_scheduling(async_binary, case):
    run_case(async_binary, case)


@pytest.fixture(scope="module")
def encoder_sources(tmp_path_factory):
    root = Path(os.environ.get("CHROMIX_CANVAS_ENCODER_BASELINES", ENCODER_BASELINES))
    paths = {platform: root / platform / "upstream" / ENCODER_CC
             for platform in ("linux", "macos", "windows")}
    if not all(path.exists() for path in paths.values()):
        pytest.skip("three-platform pre-Chromix ImageEncoder source fixtures are required")
    result = {}
    for platform, source_path in paths.items():
        original = source_path.read_bytes()
        mtime = source_path.stat().st_mtime_ns
        assert hashlib.sha256(original).hexdigest() == ENCODER_PREIMAGE_SHA256
        directory = tmp_path_factory.mktemp(f"canvas-encoder-{platform}")
        target = directory / ENCODER_CC
        target.parent.mkdir(parents=True)
        target.write_bytes(original)
        apply_patch(directory, "0124")
        result[platform] = target.read_text()
        apply_patch(directory, "0124", reverse=True)
        assert target.read_bytes() == original
        assert (source_path.read_bytes(), source_path.stat().st_mtime_ns) == (original, mtime)
    assert len(set(result.values())) == 1
    result["original"] = paths["linux"].read_text()
    buffer_path = root / "linux/upstream" / BUFFER_CC
    if not buffer_path.exists():
        pytest.skip("pre-Chromix ImageDataBuffer implementation is required")
    result["buffer"] = buffer_path.read_text()
    return result


@pytest.mark.parametrize("platform", ["linux", "macos", "windows"])
def test_encoder_patch_preimage_and_native_dispatch(encoder_sources, platform):
    original = encoder_sources["original"]
    expected = original.replace(
        '#include "third_party/blink/renderer/platform/graphics/static_bitmap_image.h"\n', ""
    ).replace('#include "third_party/blink/renderer/platform/runtime_enabled_features.h"\n', "")
    legacy = original[original.index("  if (RuntimeEnabledFeatures::"):original.index("  switch (mime_type)")]
    expected = expected.replace(legacy, "")
    assert encoder_sources[platform] == expected
    assert "writable_addr" not in expected
    assert "ShuffleSubchannelColorData" not in expected
    assert "RuntimeEnabledFeatures" not in expected


def test_real_encode_entry_chain(encoder_sources, async_sources):
    buffer = encoder_sources["buffer"]
    for signature in ("bool ImageDataBuffer::EncodeImage(", "String ImageDataBuffer::ToDataURL("):
        assert "ImageEncoder::Encode(" in function(buffer, signature)
    source = async_sources[ASYNC_CC]
    encode = function(source, "bool CanvasAsyncBlobCreator::EncodeImage(")
    assert "buffer->EncodeImage(mime_type, quality, encoded_image)" in encode
    pool = function(source, "void CanvasAsyncBlobCreator::EncodeImageOnEncoderThread(")
    assert "EncodeImage(std::move(data_buffer), mime_type, quality," in pool
    initialize = function(source, "bool CanvasAsyncBlobCreator::InitializeEncoder(")
    assert initialize.count("ImageEncoder::Create(&encoded_image_, src_data_,") == 2
    assert "ShuffleSubchannelColorData" not in encoder_sources["linux"]


def encoder_unit(patched_sources, encoder_sources, original=False):
    preamble = r'''
#define DCHECK(x) assert(x)
#define NOTREACHED() std::abort()
#define JPEG_MAX_DIMENSION 65500
#define WEBP_MAX_DIMENSION 16383
template<class T> using Vector = std::vector<T>;
enum ImageEncodingMimeType { kMimeTypePng, kMimeTypeJpeg, kMimeTypeWebp };
using String = std::string;
'''
    supported = support().replace(
        "  const SkPixmap& GetPixmap() const",
        "  bool EncodeImage(ImageEncodingMimeType, const double&, Vector<unsigned char>*) const;\n"
        "  String ToDataURL(ImageEncodingMimeType, const double&) const;\n"
        "  const SkPixmap& GetPixmap() const",
    ).replace(
        "  PaintImage paint;",
        "  static void ShuffleSubchannelColorData(void* p, const SkImageInfo&, int, int) {\n"
        "    ++legacy_shuffle_calls; *static_cast<uint8_t*>(p) ^= 1;\n"
        "  }\n  PaintImage paint;",
    ).replace("struct StaticBitmapImage :", "int legacy_shuffle_calls = 0;\nstruct StaticBitmapImage :")
    # The preamble depends only on standard types from CPP_SUPPORT.
    insertion = supported.index("namespace base {")
    supported = supported[:insertion] + preamble + supported[insertion:]
    source = encoder_sources["original" if original else "linux"]
    encoder = source[source.index("namespace blink {"):]
    methods = "\n".join(function(encoder_sources["buffer"], signature) for signature in (
        "bool ImageDataBuffer::EncodeImage(", "String ImageDataBuffer::ToDataURL("))
    return (supported + read_noise(patched_sources) + buffer_code(patched_sources) +
            ENCODER_SUPPORT + encoder + "\nnamespace blink {\n" + methods + "\n}\n" +
            ENCODER_TESTS)


@pytest.fixture(scope="module")
def encoder_binary(tmp_path_factory, patched_sources, encoder_sources):
    directory = tmp_path_factory.mktemp("canvas-real-encoder-entry")
    return compile_cpp(directory, encoder_unit(patched_sources, encoder_sources))


@pytest.mark.parametrize("case", ["borrowed", "private", "f16", "readonly", "codec-failures", "options"])
def test_actual_encoder_entry_is_readonly(encoder_binary, case):
    run_case(encoder_binary, case)


def test_original_encoder_mutates_borrowed_pixels(tmp_path, patched_sources, encoder_sources):
    binary = compile_cpp(tmp_path, encoder_unit(patched_sources, encoder_sources, original=True))
    run_case(binary, "legacy-defect")


PIXMAP_CONVERSION = r'''  bool readPixels(const SkImageInfo& info, void* dst, size_t row) const {
    if (fail_conversion) return false;
    ++conversions;
    assert(info.cs == info_.cs && info.ct == info_.ct);
    const size_t bpp = info.bytesPerPixel();
    for (int y = 0; y < info.h; ++y) for (int x = 0; x < info.w; ++x) {
      const auto* in = static_cast<const uint8_t*>(addr()) + size_t(y) * rowBytes() + size_t(x) * bpp;
      auto* out = static_cast<uint8_t*>(dst) + size_t(y) * row + size_t(x) * bpp;
      std::memcpy(out, in, bpp);
      if (info_.at != kPremul_SkAlphaType) continue;
      if (info.ct == kRGBA_F16_SkColorType) {
        _Float16 channels[4]; std::memcpy(channels, in, 8);
        float alpha = info_.at == kOpaque_SkAlphaType ? 1.0f : float(channels[3]);
        for (int c = 0; c < 3; ++c) channels[c] = alpha == 0 ? 0 : float(channels[c]) / alpha;
        channels[3] = alpha;
        std::memcpy(out, channels, 8);
      } else {
        unsigned alpha = info_.at == kOpaque_SkAlphaType ? 255 : in[3];
        for (int c = 0; c < 3; ++c) out[c] = alpha == 0 ? 0 : std::min(255u, (unsigned(in[c]) * 255 + alpha / 2) / alpha);
        out[3] = alpha;
      }
    }
    return true;
  }'''

PIXEL_TESTS = r'''
using blink::ImageDataBuffer;
struct Pixels {
  SkImageInfo info;
  size_t row;
  std::vector<uint8_t> storage;
  Pixels(SkColorType ct, SkAlphaType at, int cs = 7)
      : info{7, 4, ct, at, cs}, row(info.minRowBytes() + 16), storage(row * 4, 0xa7) {
    for (int y = 0; y < 4; ++y) for (int x = 0; x < 7; ++x) {
      auto* p = storage.data() + size_t(y) * row + size_t(x) * info.bytesPerPixel();
      if (ct == kRGBA_F16_SkColorType) {
        _Float16 v[4] = {_Float16(-0.5), _Float16(0.25), _Float16(2.0), _Float16(x == 0 ? 0 : x == 1 ? 0.5 : 1.0)};
        if (at == kOpaque_SkAlphaType) v[3] = 1;
        std::memcpy(p, v, 8);
      } else {
        p[0] = x == 2 ? 255 : 20; p[1] = 60; p[2] = 80;
        p[3] = at == kOpaque_SkAlphaType ? 255 : x == 0 ? 0 : x == 1 ? 1 : x == 2 ? 64 : 255;
      }
    }
  }
  SkPixmap pixmap() { return {info, storage.data(), row}; }
  auto image() {
    auto image = std::make_shared<StaticBitmapImage>();
    image->paint.image = std::make_shared<SkImage>();
    image->paint.image->pixels = pixmap();
    return image;
  }
};
void Equal(const SkPixmap& a, const SkPixmap& b) {
  assert(a.width() == b.width() && a.height() == b.height());
  assert(a.info().at == b.info().at && a.info().ct == b.info().ct && a.info().cs == b.info().cs);
  for (int y = 0; y < a.height(); ++y)
    assert(std::memcmp(static_cast<const uint8_t*>(a.addr()) + size_t(y) * a.rowBytes(),
                       static_cast<const uint8_t*>(b.addr()) + size_t(y) * b.rowBytes(), a.info().minRowBytes()) == 0);
}
int main(int argc, char** argv) {
  assert(argc == 2); std::string test = argv[1];
  if (test == "premul" || test == "opaque" || test == "f16" || test == "color-space") {
    for (auto ct : {kRGBA_8888_SkColorType, kBGRA_8888_SkColorType, kRGBA_F16_SkColorType})
    for (auto at : {kPremul_SkAlphaType, kOpaque_SkAlphaType}) for (int cs : {0, 1, 7})
    for (bool disabled : {false, true}) {
      base::UxrConfig::GetInstance().disabled = disabled;
      Pixels original(ct, at, cs); auto before = original.storage;
      Pixels readback(ct, kUnpremul_SkAlphaType, cs);
      assert(original.pixmap().readPixels(readback.info, readback.storage.data(), readback.row));
      ImageData data{readback.pixmap()}; ReadNoise(&data, 0, 0);
      for (int repeat = 0; repeat < 3; ++repeat) {
        conversions = 0;
        auto buffer = ImageDataBuffer::Create(original.pixmap()); assert(buffer && conversions == 1);
        Equal(buffer->GetPixmap(), readback.pixmap()); assert(original.storage == before);
        auto sync = ImageDataBuffer::Create(original.image()); assert(sync); Equal(sync->GetPixmap(), buffer->GetPixmap());
        for (int y = 0; y < 3; ++y) for (size_t x = original.info.minRowBytes(); x < original.row; ++x)
          assert(static_cast<const uint8_t*>(buffer->GetPixmap().addr())[size_t(y) * original.row + x] == 0xa7);
      }
    }
    for (const std::string seed : {"1", "4294967296", "9223372036854775808", "18446744073709551615"}) {
      base::UxrConfig::GetInstance().seed = seed;
      base::UxrConfig::GetInstance().disabled = false;
      Pixels f16(kRGBA_F16_SkColorType, kUnpremul_SkAlphaType);
      const uint16_t specials[] = {0x0001, 0x8000, 0x7c00, 0x7e01};
      std::memcpy(f16.storage.data(), specials, sizeof(specials));
      auto before = f16.storage; auto buffer = ImageDataBuffer::Create(f16.pixmap()); assert(buffer);
      assert(buffer->GetPixmap().addr() == f16.storage.data());
      ImageData data{f16.pixmap()}; ReadNoise(&data, INT_MIN, INT_MAX); assert(f16.storage == before);
    }
  } else if (test == "invalid-layout") {
    for (bool disabled : {false, true}) for (auto ct : {kRGBA_8888_SkColorType, kRGBA_F16_SkColorType}) {
      base::UxrConfig::GetInstance().disabled = disabled;
      Pixels p(ct, kUnpremul_SkAlphaType);
      for (size_t row : {size_t{0}, p.info.minRowBytes() - 1, p.info.minRowBytes() + 1, SIZE_MAX}) {
        auto pm = p.pixmap(); pm.rb = row; assert(!ImageDataBuffer::Create(pm));
      }
      auto pm = p.pixmap(); pm.address = nullptr; assert(!ImageDataBuffer::Create(pm));
    }
  } else if (test == "conversion-failure") {
    for (bool disabled : {false, true}) {
      base::UxrConfig::GetInstance().disabled = disabled;
      Pixels p(kRGBA_8888_SkColorType, kPremul_SkAlphaType); auto before = p.storage;
      fail_conversion = true;
      assert(!ImageDataBuffer::Create(p.pixmap())); assert(!ImageDataBuffer::Create(p.image()));
      assert(p.storage == before); fail_conversion = false;
    }
  } else if (test == "noise-gate") {
    Pixels p(kRGBA_8888_SkColorType, kUnpremul_SkAlphaType); auto before = p.storage;
    for (const std::string seed : {"", "0", "invalid", "18446744073709551616", "+1", "-1", " 1", "1 ", "12345"}) {
      auto& config = base::UxrConfig::GetInstance(); config.seed = seed; config.disabled = seed == "12345";
      auto b = ImageDataBuffer::Create(p.pixmap()); assert(b); Equal(b->GetPixmap(), p.pixmap()); assert(p.storage == before);
      assert(LegacyNoiseAllowed(true) == !config.disabled);
      assert(!LegacyNoiseAllowed(false));
    }
    auto& config = base::UxrConfig::GetInstance(); config.disabled = false;
    for (const std::string seed : {"1", "4294967296", "9223372036854775808", "18446744073709551615"}) {
      config.seed = seed;
      assert(!LegacyNoiseAllowed(true));
      auto encoded = ImageDataBuffer::Create(p.pixmap()); assert(encoded);
      Pixels read = p; ImageData data{read.pixmap()}; ReadNoise(&data, 0, 0);
      Equal(encoded->GetPixmap(), read.pixmap()); assert(read.storage != before && p.storage == before);
    }
    config.seed = ""; legacy_noise = false; assert(!LegacyNoiseAllowed(true));
    legacy_noise = true; config.synthetic = false;
    assert(!LegacyNoiseAllowed(true));
    config.seed = "12345";
    assert(!LegacyNoiseAllowed(true));
  } else return 2;
}
'''

ASYNC_SUPPORT = r'''
#include <cmath>
#include <functional>
#include <tuple>
#define CHECK(x) assert(x)
#define CHECK_LE(x, y) assert((x) <= (y))
#define DCHECK(x) assert(x)
#define DCHECK_EQ(x, y) assert((x) == (y))
#define FROM_HERE 0
namespace base {
struct TimeTicks { static TimeTicks Now() { return {}; } };
int operator-(TimeTicks, TimeTicks) { return 0; }
}
bool main_thread = true, yield_rows = false, fail_rows = false;
bool IsMainThread() { return main_thread; }
constexpr int kIdleTaskStartTimeoutDelayMs = 1000, kIdleTaskCompleteTimeoutDelayMs = 5700;
bool IsEncodeRowDeadlineNearOrPassed(base::TimeTicks, size_t) { return yield_rows; }
bool IsCreateBlobDeadlineNearOrPassed(base::TimeTicks) { return false; }
void RecordInitiateEncodingTimeHistogram(int, int) {}
void RecordCompleteEncodingTimeHistogram(int, int) {}
namespace base { using TimeDelta = int; }
template<class T> struct Member {
  T* p = nullptr;
  Member() = default; Member(T* value) : p(value) {}
  T* operator->() const { assert(p); return p; }
  void Clear() { p = nullptr; }
};
struct TaskRunner {
  std::vector<std::function<void()>> tasks;
  template<class F> void PostTask(int, F f) {
    auto saved = std::make_shared<F>(std::move(f)); tasks.push_back([saved] { (*saved)(); });
  }
  void Run() { auto copy = std::move(tasks); tasks.clear(); for (auto& task : copy) task(); }
};
namespace base { using SingleThreadTaskRunner = TaskRunner; }
enum class TaskType { kCanvasBlobSerialization };
struct ExecutionContext {
  bool window = true; std::shared_ptr<TaskRunner> runner = std::make_shared<TaskRunner>();
  bool IsWindow() const { return window; }
  bool IsContextThread() const { return true; }
  auto GetTaskRunner(TaskType) { return runner; }
};
struct Blob {};
template<class T> struct ScriptPromiseResolver {};
struct V8BlobCallback {};
struct ImageEncodeOptions { int mime = 0; int type() const { return mime; } };
constexpr int kMimeTypePng = 0, kMimeTypeJpeg = 1, kMimeTypeWebp = 2;
using ImageEncodingMimeType = int;
struct ImageEncoderUtils {
  static constexpr int kEncodeReasonConvertToBlobPromise = 0;
  static int ToEncodingMimeType(int type, int) { return type; }
};
template<class T> using Vector = std::vector<T>;
template<class T> T* WrapPersistent(T* value) { return value; }
template<class T> T* MakeCrossThreadHandle(T* value) { return value; }
template<class F, class T, class... Args> auto BindOnce(F f, T* object, Args&&... args) {
  return [f, object, args = std::make_tuple(std::forward<Args>(args)...)]() mutable {
    std::apply([&](auto&&... unpacked) {
      if constexpr (std::is_invocable_v<F, T*, decltype(unpacked)...>)
        std::invoke(f, object, std::move(unpacked)...);
      else
        std::invoke(f, object, std::move(unpacked)..., base::TimeTicks{});
    }, args);
  };
}
template<class F, class... Args> auto CrossThreadBindOnce(F f, Args&&... args) {
  return [f, args = std::make_tuple(std::forward<Args>(args)...)]() mutable {
    std::apply(f, std::move(args));
  };
}
namespace worker_pool {
TaskRunner runner;
template<class F> void PostTask(int, F task) { runner.PostTask(0, std::move(task)); }
}
struct ThreadScheduler {
  static ThreadScheduler* Current() { static ThreadScheduler scheduler; return &scheduler; }
  template<class F, class T> void PostIdleTask(int, F, T) {}
  template<class F> void PostIdleTask(int, F) {}
};
namespace SkJpegEncoder {
enum class AlphaOption { kBlendOnBlack };
enum class Downsample { k444 };
struct Options { int fQuality = 0; AlphaOption fAlphaOption{}; Downsample fDownsample{}; };
}
namespace SkPngRustEncoder { enum class CompressionLevel { kLow }; }
struct ImageEncoder {
  SkPixmap pm; std::vector<unsigned char>* output; int row = 0;
  static int MaxDimension(int) { return 4; }
  static int ComputeJpegQuality(double quality) { return int(quality * 100); }
  template<class Options> static auto Create(std::vector<unsigned char>* out, SkPixmap pm, Options) {
    return std::make_unique<ImageEncoder>(ImageEncoder{pm, out});
  }
  bool encodeRows(int count) {
    if (fail_rows) return false;
    for (int i = 0; i < count; ++i, ++row) {
      const auto* begin = static_cast<const uint8_t*>(pm.addr()) + size_t(row) * pm.rowBytes();
      output->insert(output->end(), begin, begin + pm.info().minRowBytes());
    }
    return true;
  }
};
namespace blink {
using ::BindOnce;
class CanvasAsyncBlobCreator {
 public:
  enum IdleTaskStatus { kIdleTaskNotStarted, kIdleTaskStarted, kIdleTaskCompleted, kIdleTaskFailed,
                        kIdleTaskSwitchedToImmediateTask, kIdleTaskNotSupported };
  enum ToBlobFunctionType { kHTMLCanvasToBlobCallback, kOffscreenCanvasConvertToBlobPromise };
  CanvasAsyncBlobCreator(scoped_refptr<StaticBitmapImage>, const ImageEncodeOptions*, ToBlobFunctionType,
                        V8BlobCallback*, base::TimeTicks, ExecutionContext*, ScriptPromiseResolver<Blob>*);
  IdleTaskStatus idle_task_status_{};
  bool fail_encoder_initialization_for_test_, enforce_idle_encoding_for_test_;
  Member<ExecutionContext> context_;
  ToBlobFunctionType function_type_;
  base::TimeTicks start_time_;
  bool static_bitmap_image_loaded_;
  Member<V8BlobCallback> callback_;
  Member<ScriptPromiseResolver<Blob>> script_promise_resolver_;
  scoped_refptr<StaticBitmapImage> image_;
  sk_sp<SkImage> skia_image_;
  std::unique_ptr<ImageDataBuffer> image_data_buffer_;
  SkPixmap src_data_;
  std::unique_ptr<ImageEncoder> encoder_;
  Vector<unsigned char> encoded_image_;
  int num_rows_completed_ = 0;
  ImageEncodingMimeType mime_type_ = 0;
  base::TimeTicks schedule_idle_task_start_time_;
  std::shared_ptr<TaskRunner> parent_frame_task_runner_;
  int successes = 0, failures = 0, signals = 0;
  std::vector<uint8_t> result;
  void Dispose(); void ScheduleAsyncBlobCreation(const double&); void InitiateEncoding(double, base::TimeTicks);
  void IdleEncodeRows(base::TimeTicks); void ForceEncodeRows(); bool InitializeEncoder(double);
  void IdleTaskStartTimeoutEvent(double); void IdleTaskCompleteTimeoutEvent();
  void ScheduleInitiateEncoding(double) {}
  template<class F> void PostDelayedTaskToCurrentThread(int, F, int) {}
  void SignalTaskSwitchInStartTimeoutEventForTesting() { ++signals; }
  void SignalTaskSwitchInCompleteTimeoutEventForTesting() { ++signals; }
  void SignalAlternativeCodePathFinishedForTesting() { ++signals; }
  void CreateNullAndReturnResult() { ++failures; Dispose(); }
  void CreateBlobAndReturnResult(Vector<unsigned char> bytes) { ++successes; result = std::move(bytes); Dispose(); }
  static bool EncodeImage(std::unique_ptr<ImageDataBuffer> buffer, int, const double&, Vector<unsigned char>* out) {
    if (!buffer) return false;
    auto encoder = ImageEncoder::Create(out, buffer->GetPixmap(), 0);
    return encoder->encodeRows(buffer->GetPixmap().height());
  }
  static void EncodeImageOnEncoderThread(CanvasAsyncBlobCreator* creator, std::shared_ptr<TaskRunner> runner,
                                         sk_sp<SkImage> image, std::unique_ptr<ImageDataBuffer> buffer, int mime, double quality) {
    Vector<unsigned char> bytes;
    if (!EncodeImage(std::move(buffer), mime, quality, &bytes)) creator->CreateNullAndReturnResult();
    else creator->CreateBlobAndReturnResult(std::move(bytes));
  }
};
}
'''

ASYNC_TESTS = r'''
using blink::CanvasAsyncBlobCreator;
std::vector<uint8_t> Tight(const SkPixmap& pm) {
  std::vector<uint8_t> result;
  for (int y = 0; y < pm.height(); ++y) {
    const auto* begin = static_cast<const uint8_t*>(pm.addr()) + size_t(y) * pm.rowBytes();
    result.insert(result.end(), begin, begin + pm.info().minRowBytes());
  }
  return result;
}
int main(int argc, char** argv) {
  assert(argc == 2); std::string test = argv[1];
  for (const std::string seed : {"1", "12345", "4294967296", "9223372036854775808", "18446744073709551615"})
  for (bool disabled : {false, true}) for (auto ct : {kRGBA_8888_SkColorType, kBGRA_8888_SkColorType, kRGBA_F16_SkColorType})
  for (auto at : {kUnpremul_SkAlphaType, kPremul_SkAlphaType, kOpaque_SkAlphaType}) {
    base::UxrConfig::GetInstance().seed = seed;
    base::UxrConfig::GetInstance().disabled = disabled;
    main_thread = test != "worker"; no_idle = test == "no-idle"; yield_rows = false;
    ExecutionContext context; context.window = main_thread;
    ImageEncodeOptions options; options.mime = test == "encoder-pool" ? kMimeTypeWebp : test == "idle-jpeg" ? kMimeTypeJpeg : kMimeTypePng;
    SkImageInfo info{7, 3, ct, at, 9}; size_t row = info.minRowBytes() + 16;
    std::vector<uint8_t> bytes(row * 3, 0xa7);
    for (int y = 0; y < 3; ++y) for (int x = 0; x < 7; ++x) {
      auto* p = bytes.data() + size_t(y) * row + size_t(x) * info.bytesPerPixel();
      if (ct == kRGBA_F16_SkColorType) { _Float16 v[4] = {0.125, 0.25, 0.5, _Float16(at == kOpaque_SkAlphaType ? 1 : 0.5)}; std::memcpy(p, v, 8); }
      else { p[0] = 20; p[1] = 60; p[2] = 100; p[3] = at == kOpaque_SkAlphaType ? 255 : x == 0 ? 0 : x == 1 ? 1 : x == 2 ? 128 : 255; }
    }
    auto before = bytes;
    auto image = std::make_shared<StaticBitmapImage>();
    image->paint.image = std::make_shared<SkImage>(); image->paint.image->pixels = {info, bytes.data(), row};
    auto crop = image->paint.image->pixels; crop.info_.w = 4;
    auto sync = blink::ImageDataBuffer::Create(crop); assert(sync); auto expected = Tight(sync->GetPixmap());
    allocation_count = 0; fail_allocation = test == "allocation-failure" ? 1 : 0;
    CanvasAsyncBlobCreator creator(image, &options, main_thread ? CanvasAsyncBlobCreator::kHTMLCanvasToBlobCallback : CanvasAsyncBlobCreator::kOffscreenCanvasConvertToBlobPromise,
                                   nullptr, {}, &context, nullptr);
    fail_allocation = 0;
    const bool failed_prepare = !creator.static_bitmap_image_loaded_;
    if (!failed_prepare) {
      assert(creator.src_data_.width() == 4 && creator.src_data_.info().cs == 9);
      assert(Tight(creator.src_data_) == expected);
    }
    assert(bytes == before);
    const int prepare_allocations = allocation_count;
    if (test == "snapshot-lifetime" && creator.image_data_buffer_ && creator.image_data_buffer_->retained_image_) {
      bytes.assign(bytes.size(), 0xcc); creator.image_ = nullptr; creator.skia_image_ = nullptr; image = nullptr;
    }
    creator.fail_encoder_initialization_for_test_ = test == "init-failure" || test == "start-init-failure";
    creator.ScheduleAsyncBlobCreation(0.8);
    if (!failed_prepare && main_thread && options.mime != kMimeTypeWebp && !no_idle) {
      if (test == "start-timeout" || test == "start-init-failure") {
        creator.IdleTaskStartTimeoutEvent(0.8);
        creator.InitiateEncoding(0.8, {});
      } else {
        yield_rows = test == "complete-timeout";
        fail_rows = test == "row-failure";
        creator.InitiateEncoding(0.8, {});
        if (yield_rows) { creator.IdleTaskCompleteTimeoutEvent(); creator.IdleEncodeRows({}); yield_rows = false; }
      }
    }
    worker_pool::runner.Run(); context.runner->Run(); fail_rows = false;
    if (main_thread && options.mime != kMimeTypeWebp && !no_idle && !failed_prepare) {
      creator.InitiateEncoding(0.8, {});
      creator.IdleEncodeRows({});
    }
    assert(allocation_count == prepare_allocations);
    bool failure = failed_prepare || test == "init-failure" || test == "start-init-failure" || test == "row-failure";
    assert(creator.failures == int(failure) && creator.successes == int(!failure));
    if (!failure) assert(creator.result == expected);
    assert(!creator.image_data_buffer_ && !creator.encoder_ && creator.src_data_.addr() == nullptr);
  }
}
'''

ENCODER_SUPPORT = r'''
#include <cmath>
#include <initializer_list>
#include <sys/mman.h>
#include <unistd.h>
namespace blink {
class VectorWStream {
 public:
  explicit VectorWStream(Vector<unsigned char>* dst) : dst_(dst) { assert(dst && dst->empty()); }
  bool write(const void* data, size_t size) {
    const auto* bytes = static_cast<const uint8_t*>(data);
    dst_->insert(dst_->end(), bytes, bytes + size); return true;
  }
 private:
  Vector<unsigned char>* dst_;
};
}
bool codec_fail = false;
int last_codec = -1;
double last_quality = 0;
bool last_blend = false, last_444 = false, last_lossless = false;
struct SkEncoder {
  blink::VectorWStream* stream;
  SkPixmap pixels;
  int row = 0;
  bool encodeRows(int count) {
    if (codec_fail) return false;
    assert(row + count <= pixels.height());
    for (int end = row + count; row < end; ++row)
      stream->write(static_cast<const uint8_t*>(pixels.addr()) + size_t(row) * pixels.rowBytes(), pixels.info().minRowBytes());
    return true;
  }
};
namespace SkJpegEncoder {
enum class AlphaOption { kIgnore, kBlendOnBlack };
enum class Downsample { k420, k444 };
struct Options { int fQuality = 100; AlphaOption fAlphaOption = AlphaOption::kIgnore; Downsample fDownsample = Downsample::k420; };
std::unique_ptr<SkEncoder> Make(blink::VectorWStream* dst, const SkPixmap& src, const Options& options) {
  last_codec = kMimeTypeJpeg; last_quality = options.fQuality;
  last_blend = options.fAlphaOption == AlphaOption::kBlendOnBlack;
  last_444 = options.fDownsample == Downsample::k444;
  if (codec_fail) return nullptr;
  return std::make_unique<SkEncoder>(SkEncoder{dst, src});
}
bool Encode(blink::VectorWStream* dst, const SkPixmap& src, const Options& options) {
  auto encoder = Make(dst, src, options); return encoder && encoder->encodeRows(src.height());
}
}
namespace SkPngRustEncoder {
enum class CompressionLevel { kLow };
struct Options { CompressionLevel fCompressionLevel; };
std::unique_ptr<SkEncoder> Make(blink::VectorWStream* dst, const SkPixmap& src, const Options& options) {
  assert(options.fCompressionLevel == CompressionLevel::kLow); last_codec = kMimeTypePng;
  if (codec_fail) return nullptr;
  return std::make_unique<SkEncoder>(SkEncoder{dst, src});
}
bool Encode(blink::VectorWStream* dst, const SkPixmap& src, const Options& options) {
  auto encoder = Make(dst, src, options); return encoder && encoder->encodeRows(src.height());
}
}
namespace SkWebpEncoder {
enum class Compression { kLossy, kLossless };
struct Options { Compression fCompression = Compression::kLossy; float fQuality = 75; };
bool Encode(blink::VectorWStream* dst, const SkPixmap& src, const Options& options) {
  last_codec = kMimeTypeWebp; last_quality = options.fQuality;
  last_lossless = options.fCompression == Compression::kLossless;
  SkEncoder encoder{dst, src}; return encoder.encodeRows(src.height());
}
}
namespace blink {
class ImageEncoder {
 public:
  static bool Encode(Vector<unsigned char>*, const SkPixmap&, const SkJpegEncoder::Options&);
  static bool Encode(Vector<unsigned char>*, const SkPixmap&, SkPngRustEncoder::CompressionLevel);
  static bool Encode(Vector<unsigned char>*, const SkPixmap&, const SkWebpEncoder::Options&);
  static bool Encode(Vector<unsigned char>*, const SkPixmap&, ImageEncodingMimeType, double);
  static std::unique_ptr<ImageEncoder> Create(Vector<unsigned char>*, const SkPixmap&, const SkJpegEncoder::Options&);
  static std::unique_ptr<ImageEncoder> Create(Vector<unsigned char>*, const SkPixmap&, SkPngRustEncoder::CompressionLevel);
  static int MaxDimension(ImageEncodingMimeType);
  static int ComputeJpegQuality(double);
  static SkWebpEncoder::Options ComputeWebpOptions(double);
  bool encodeRows(int rows) { return encoder_->encodeRows(rows); }
 private:
  explicit ImageEncoder(Vector<unsigned char>* dst) : dst_(dst) {}
  VectorWStream dst_;
  std::unique_ptr<SkEncoder> encoder_;
};
struct ImageEncoderUtils {
  static String MimeTypeName(ImageEncodingMimeType mime) {
    switch (mime) {
      case kMimeTypePng: return "image/png";
      case kMimeTypeJpeg: return "image/jpeg";
      case kMimeTypeWebp: return "image/webp";
    }
    std::abort();
  }
};
String Base64Encode(const Vector<unsigned char>& bytes) {
  String result; const char* hex = "0123456789abcdef";
  for (auto byte : bytes) { result += hex[byte >> 4]; result += hex[byte & 15]; }
  return result;
}
String StrCat(std::initializer_list<String> parts) {
  String result; for (const auto& part : parts) result += part; return result;
}
}
'''

ENCODER_TESTS = r'''
using blink::ImageDataBuffer;
using blink::ImageEncoder;
std::vector<uint8_t> ActivePixels(const SkPixmap& pm) {
  std::vector<uint8_t> result;
  for (int y = 0; y < pm.height(); ++y) {
    const auto* row = static_cast<const uint8_t*>(pm.addr()) + size_t(y) * pm.rowBytes();
    result.insert(result.end(), row, row + pm.info().minRowBytes());
  }
  return result;
}
int main(int argc, char** argv) {
  assert(argc == 2); const std::string test = argv[1];
  if (test == "legacy-defect") {
    std::vector<uint8_t> bytes(16 * 16 * 4);
    for (size_t i = 0; i < bytes.size(); i += 4) {
      bytes[i] = uint8_t(40 + i % 31); bytes[i + 1] = 80; bytes[i + 2] = 120; bytes[i + 3] = 255;
    }
    const SkPixmap pm({16, 16}, bytes.data(), 16 * 4);
    base::UxrConfig::GetInstance().disabled = true; legacy_noise = true;
    auto borrowed = ImageDataBuffer::Create(pm); assert(borrowed && borrowed->GetPixmap().addr() == bytes.data());
    Vector<unsigned char> out;
    assert(borrowed->EncodeImage(kMimeTypePng, 0.8, &out));
    assert(bytes[0] != 40 && legacy_shuffle_calls == 1);
    return 0;
  }
  for (bool legacy : {false, true}) for (bool disabled : {false, true})
  for (const std::string seed : {"", "0", "invalid", "12345", "18446744073709551615"})
  for (auto ct : {kRGBA_8888_SkColorType, kBGRA_8888_SkColorType, kRGBA_F16_SkColorType}) {
    auto& config = base::UxrConfig::GetInstance(); config.seed = seed; config.disabled = disabled;
    legacy_noise = legacy; legacy_shuffle_calls = 0;
    SkImageInfo info{7, 3, ct, kUnpremul_SkAlphaType, 7}; size_t row = info.minRowBytes() + 16;
    std::vector<uint8_t> pixels(row * 3, 0xa7);
    for (int y = 0; y < 3; ++y) for (int x = 0; x < 7; ++x) {
      auto* p = pixels.data() + size_t(y) * row + size_t(x) * info.bytesPerPixel();
      if (ct == kRGBA_F16_SkColorType) {
        _Float16 value[4] = {-0.5, 0.25, 2, 1}; std::memcpy(p, value, 8);
      } else { p[0] = 40; p[1] = 80; p[2] = 120; p[3] = x == 0 ? 0 : x == 1 ? 1 : 255; }
    }
    const auto original = pixels;
    const SkPixmap pm(info, pixels.data(), row);
    auto buffer = ImageDataBuffer::Create(pm); assert(buffer);
    bool noise = !disabled && (seed == "12345" || seed == "18446744073709551615") && ct != kRGBA_F16_SkColorType;
    assert((buffer->GetPixmap().addr() != pm.addr()) == noise);
    const auto prepared = ActivePixels(buffer->GetPixmap());
    const auto prepared_span = gfx::SkPixmapToSpan(buffer->GetPixmap());
    const std::vector<uint8_t> prepared_storage(prepared_span.begin(), prepared_span.end());
    if (test == "borrowed" && noise) continue;
    if (test == "private" && !noise) continue;
    if (test == "f16" && ct != kRGBA_F16_SkColorType) continue;
    if (noise) assert(prepared != ActivePixels(pm)); else assert(prepared == ActivePixels(pm));
    auto readback = pixels; ImageData read{{info, readback.data(), row}}; ReadNoise(&read, 0, 0);
    assert(prepared == ActivePixels(read.pm));
    for (auto mime : {kMimeTypePng, kMimeTypeJpeg, kMimeTypeWebp}) for (int repeat = 0; repeat < 3; ++repeat) {
      Vector<unsigned char> direct;
      assert(ImageEncoder::Encode(&direct, pm, mime, 0.8));
      assert(last_codec == mime && direct == ActivePixels(pm));
      Vector<unsigned char> encoded;
      assert(buffer->EncodeImage(mime, 0.8, &encoded) && encoded == prepared);
      String expected_url = "data:" + blink::ImageEncoderUtils::MimeTypeName(mime) + ";base64," + blink::Base64Encode(prepared);
      assert(buffer->ToDataURL(mime, 0.8) == expected_url);
      if (mime != kMimeTypeWebp) {
        Vector<unsigned char> idle;
        auto encoder = mime == kMimeTypePng
            ? ImageEncoder::Create(&idle, buffer->GetPixmap(), SkPngRustEncoder::CompressionLevel::kLow)
            : ImageEncoder::Create(&idle, buffer->GetPixmap(), SkJpegEncoder::Options{});
        assert(encoder && encoder->encodeRows(1) && encoder->encodeRows(2));
        assert(idle == prepared);
      }
      assert(pixels == original && ActivePixels(buffer->GetPixmap()) == prepared && legacy_shuffle_calls == 0);
    }
    if (test == "codec-failures") {
      codec_fail = true;
      for (auto mime : {kMimeTypePng, kMimeTypeJpeg, kMimeTypeWebp}) {
        Vector<unsigned char> out;
        assert(!buffer->EncodeImage(mime, 0.8, &out));
        assert(buffer->ToDataURL(mime, 0.8) == "data:,");
        assert(!ImageEncoder::Create(&out, buffer->GetPixmap(), SkPngRustEncoder::CompressionLevel::kLow));
        assert(!ImageEncoder::Create(&out, buffer->GetPixmap(), SkJpegEncoder::Options{}));
      }
      codec_fail = false;
    }
    if (test == "readonly") {
      size_t page = size_t(sysconf(_SC_PAGESIZE)); assert(pixels.size() < page);
      void* mapping = mmap(nullptr, page, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
      assert(mapping != MAP_FAILED); std::memcpy(mapping, pixels.data(), pixels.size());
      assert(mprotect(mapping, page, PROT_READ) == 0);
      {
        SkPixmap readonly(info, mapping, row);
        auto read_only_buffer = ImageDataBuffer::Create(readonly); assert(read_only_buffer);
        for (auto mime : {kMimeTypePng, kMimeTypeJpeg, kMimeTypeWebp}) {
          Vector<unsigned char> out; assert(ImageEncoder::Encode(&out, readonly, mime, 0.8));
          assert(out == ActivePixels(pm));
          out.clear(); assert(read_only_buffer->EncodeImage(mime, 0.8, &out)); assert(out == prepared);
        }
        assert(std::memcmp(mapping, pixels.data(), pixels.size()) == 0);
      }
      assert(munmap(mapping, page) == 0);
    }
    assert(pixels == original && ActivePixels(buffer->GetPixmap()) == prepared && legacy_shuffle_calls == 0);
    const auto final_span = gfx::SkPixmapToSpan(buffer->GetPixmap());
    assert(std::equal(prepared_storage.begin(), prepared_storage.end(), final_span.begin(), final_span.end()));
  }
  if (test == "options") {
    uint8_t pixel[4] = {40, 80, 120, 255}; SkPixmap pm({1, 1}, pixel, 4);
    for (double quality : {-1.0, 0.0, 0.505, 1.0, 2.0, std::numeric_limits<double>::quiet_NaN()}) {
      Vector<unsigned char> jpeg; assert(ImageEncoder::Encode(&jpeg, pm, kMimeTypeJpeg, quality));
      int expected = quality >= 0 && quality <= 1 ? int(quality * 100 + 0.5) : 92;
      assert(last_quality == expected && last_blend && last_444 == (expected == 100));
      Vector<unsigned char> webp; assert(ImageEncoder::Encode(&webp, pm, kMimeTypeWebp, quality));
      double expected_webp = quality == 1 ? 75 : quality >= 0 && quality <= 1 ? float(quality * 100) : 80;
      assert(last_quality == expected_webp && last_lossless == (quality == 1));
    }
    assert(ImageEncoder::MaxDimension(kMimeTypePng) == 65535);
    assert(ImageEncoder::MaxDimension(kMimeTypeJpeg) == JPEG_MAX_DIMENSION);
    assert(ImageEncoder::MaxDimension(kMimeTypeWebp) == WEBP_MAX_DIMENSION);
  }
}
'''
