[CmdletBinding()]
param(
  [Parameter(Mandatory)] [string]$Src,
  [string]$OutDir = (Join-Path $Src "out\Chromix")
)
$ErrorActionPreference = "Stop"

function Set-SourceReplacement {
  param(
    [Parameter(Mandatory)] [string]$RelativePath,
    [Parameter(Mandatory)] [string]$OldText,
    [Parameter(Mandatory)] [AllowEmptyString()] [string]$NewText,
    [string[]]$CurrentMarker = @(),
    [switch]$PreferCurrentMarker
  )
  $path = Join-Path $Src $RelativePath
  if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
    throw "resume source file is missing: $RelativePath"
  }
  $content = [IO.File]::ReadAllText($path)
  if ($PreferCurrentMarker) {
    if ($NewText -ne "" -and $content.Contains($NewText)) {
      Write-Host "==> restored source already current: $RelativePath"
      return
    }
    foreach ($marker in $CurrentMarker) {
      if ($marker -ne "" -and $content.Contains($marker)) {
        Write-Host "==> restored source already current: $RelativePath"
        return
      }
    }
  }
  if ($content.Contains($OldText)) {
    [IO.File]::WriteAllText($path, $content.Replace($OldText, $NewText))
    Write-Host "==> updated restored source: $RelativePath"
    return
  }
  if ($NewText -ne "" -and $content.Contains($NewText)) {
    Write-Host "==> restored source already current: $RelativePath"
    return
  }
  foreach ($marker in $CurrentMarker) {
    if ($marker -ne "" -and $content.Contains($marker)) {
      Write-Host "==> restored source already current: $RelativePath"
      return
    }
  }
  if ($NewText -eq "") {
    Write-Host "==> restored source migration already current or not applicable: $RelativePath"
    return
  }
  Write-Host "==> restored source migration already current or not applicable: $RelativePath"
  return
}

Set-SourceReplacement `
  -RelativePath "third_party\blink\renderer\modules\mediarecorder\media_recorder.cc" `
  -OldText "const std::string ph_type = type.LowerASCII().Utf8();" `
  -NewText "const std::string ph_type = type.ToAsciiLower().Utf8();"

Set-SourceReplacement `
  -RelativePath "chrome\browser\component_updater\widevine_cdm_component_installer.cc" `
  -OldText '#include "base/json/json_file_value_deserializer.h"  // UXR' `
  -NewText '#include "base/json/json_file_value_serializer.h"  // UXR'

Set-SourceReplacement `
  -RelativePath "chrome\browser\component_updater\widevine_cdm_component_installer.cc" `
  -OldText "const base::Value::Dict* ph_dict =" `
  -NewText "const base::DictValue* ph_dict ="

Set-SourceReplacement `
  -RelativePath "third_party\blink\renderer\modules\webgl\webgl_rendering_context_base.cc" `
  -OldText '#include "base/command_line.h"  // UXR' `
  -NewText ""

Set-SourceReplacement `
  -RelativePath "third_party\blink\renderer\modules\webgl\webgl_rendering_context_base.cc" `
  -OldText '#include "base/strings/string_number_conversions.h"  // UXR' `
  -NewText ""

Set-SourceReplacement `
  -RelativePath "third_party\blink\renderer\modules\webgl\webgl_rendering_context_base.cc" `
  -OldText @'
// UXR Layer 2: seeded sub-pixel farble of WebGL readback so the render-hash is
// non-constant per session and not a known-attributable (SwiftShader) hash. PARTIAL
// mitigation only: matching a CLAIMED GPU hash still needs a real GPU. Keyed to the
// shared visual seed --uxr-canvas-seed (same as canvas 2D).
void UxrFarbleReadPixels(uint8_t* data,
                            GLsizei width,
                            GLsizei height,
                            GLenum format,
                            GLenum type) {
  // Byte-wise perturbation is only valid for tightly packed RGBA8. Other
  // formats (FLOAT, HALF_FLOAT, packed integers, RGB, RED, etc.) require
  // component-aware handling; leave them native rather than corrupting data.
  if (data == nullptr || width <= 0 || height <= 0 || format != GL_RGBA ||
      type != GL_UNSIGNED_BYTE) {
    return;
  }
  base::CheckedNumeric<size_t> checked_size = width;
  checked_size *= height;
  checked_size *= 4u;
  if (!checked_size.IsValid()) {
    return;
  }
  const size_t n = checked_size.ValueOrDie();
  if (n == 0u) {
    return;
  }
  if (!base::UxrConfig::GetInstance().Has("uxr-canvas-seed")) {
    return;
  }
  uint32_t seed = 0u;
  if (!base::StringToUint(base::UxrConfig::GetInstance().Get("uxr-canvas-seed"), &seed) ||
      seed == 0u) {
    return;
  }
  base::span<uint8_t> px = UNSAFE_BUFFERS(base::span<uint8_t>(data, n));
  for (size_t i = 0; i < n; ++i) {
    if ((i & 3u) == 3u) continue;  // skip alpha (RGBA); same shape as canvas2D noise
    const uint32_t ch = static_cast<uint32_t>(i & 3u);
    const uint32_t v = px[i];
    uint32_t z = seed ^ (static_cast<uint32_t>(i >> 2) * 374761393u) ^
                 (ch * 0x9e3779b9u) ^ (v * 2654435761u);  // value-dependent, per-channel
    z ^= z >> 16; z *= 0x85ebca6bu; z ^= z >> 13; z *= 0xc2b2ae35u;
    z ^= z >> 16;
    const int nv = static_cast<int>(v) + ((z & 1u) ? 1 : -1);
    px[i] = static_cast<uint8_t>(nv < 0 ? 0 : (nv > 255 ? 255 : nv));
  }
}

'@ `
  -NewText ""

Set-SourceReplacement `
  -RelativePath "third_party\blink\renderer\modules\webgl\webgl_rendering_context_base.cc" `
  -OldText @'
  // UXR patch #3b: coherent WebGL persona (full-params, not string-only).
  // Detectors read the numeric getParameter limits and cross-check them vs the
  // renderer string (rules C2/C3). When --uxr-webgl-fullparams is set we return
  // values coherent with a real NVIDIA desktop GPU (ANGLE-D3D11).
  {
    if ((base::UxrConfig::GetInstance().Has("uxr-webgl-fullparams") || base::UxrConfig::GetInstance().Has("uxr-webgl-renderer"))) {
      switch (pname) {
        case GL_MAX_TEXTURE_SIZE:
        case GL_MAX_CUBE_MAP_TEXTURE_SIZE:
        case GL_MAX_RENDERBUFFER_SIZE:
          return WebGLAny(script_state, 16384);
        case GL_MAX_VERTEX_ATTRIBS:
        case GL_MAX_VERTEX_TEXTURE_IMAGE_UNITS:
        case GL_MAX_TEXTURE_IMAGE_UNITS:
          return WebGLAny(script_state, 16);
        case GL_MAX_COMBINED_TEXTURE_IMAGE_UNITS:
          return WebGLAny(script_state, 32);
        case GL_MAX_VERTEX_UNIFORM_VECTORS:
          return WebGLAny(script_state, 4096);
        case GL_MAX_VARYING_VECTORS:
          return WebGLAny(script_state, 30);
        case GL_MAX_FRAGMENT_UNIFORM_VECTORS:
          return WebGLAny(script_state, 1024);
        default:
          break;
      }
    }
  }
'@ `
  -NewText ""

Set-SourceReplacement `
  -RelativePath "third_party\blink\renderer\modules\webgl\webgl_rendering_context_base.cc" `
  -OldText @'
      {
        // UXR: kill real-backend GLSL leak; match real Chrome normalized form.
        if ((base::UxrConfig::GetInstance().Has("uxr-webgl-fullparams") || base::UxrConfig::GetInstance().Has("uxr-webgl-renderer")))
          return WebGLAny(script_state,
              String("WebGL GLSL ES 1.0 (OpenGL ES GLSL ES 1.0 Chromium)"));
      }
'@ `
  -NewText ""

Set-SourceReplacement `
  -RelativePath "third_party\blink\renderer\modules\webgl\webgl_rendering_context_base.cc" `
  -OldText @'
      {
        // UXR: kill real-backend driver leak (SwiftShader/host GL). Real Chrome
        // reports the normalized "OpenGL ES 2.0 Chromium" form regardless of GPU.
        if ((base::UxrConfig::GetInstance().Has("uxr-webgl-fullparams") || base::UxrConfig::GetInstance().Has("uxr-webgl-renderer")))
          return WebGLAny(script_state, String("WebGL 1.0 (OpenGL ES 2.0 Chromium)"));
      }
'@ `
  -NewText ""

Set-SourceReplacement `
  -RelativePath "third_party\blink\renderer\modules\webgl\webgl_rendering_context_base.cc" `
  -OldText @'
        // UXR patch #3: switch-driven renderer (dynamic persona).
        {
          if (base::UxrConfig::GetInstance().Has("uxr-webgl-renderer"))
            return WebGLAny(script_state, String(base::UxrConfig::GetInstance().Get("uxr-webgl-renderer").c_str()));
        }
        return WebGLAny(
            script_state,
            String("ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Direct3D11 vs_5_0 "
                   "ps_5_0, D3D11)"));
'@ `
  -NewText @'
        const auto& config = base::UxrConfig::GetInstance();
        const std::string renderer = config.Get("uxr-webgl-renderer");
        const std::string vendor = config.Get("uxr-webgl-vendor");
        if (!renderer.empty() && !vendor.empty()) {
          return WebGLAny(script_state, String(renderer.c_str()));
        }
'@ `
  -CurrentMarker @(
    'const std::string renderer = config.Get("uxr-webgl-renderer");',
    'String(renderer.c_str())',
    'String(WebGLPersonaRenderer().c_str())'
  )

Set-SourceReplacement `
  -RelativePath "third_party\blink\renderer\modules\webgl\webgl_rendering_context_base.cc" `
  -OldText @'
        // UXR patch #3: switch-driven vendor (dynamic persona).
        {
          if (base::UxrConfig::GetInstance().Has("uxr-webgl-vendor"))
            return WebGLAny(script_state, String(base::UxrConfig::GetInstance().Get("uxr-webgl-vendor").c_str()));
        }
        return WebGLAny(script_state, String("Google Inc. (NVIDIA Corporation)"));
'@ `
  -NewText @'
        const auto& config = base::UxrConfig::GetInstance();
        const std::string renderer = config.Get("uxr-webgl-renderer");
        const std::string vendor = config.Get("uxr-webgl-vendor");
        if (!renderer.empty() && !vendor.empty()) {
          return WebGLAny(script_state, String(vendor.c_str()));
        }
'@ `
  -CurrentMarker @(
    'const std::string vendor = config.Get("uxr-webgl-vendor");',
    'String(vendor.c_str())',
    'String(WebGLPersonaVendor().c_str())'
  )

Set-SourceReplacement `
  -RelativePath "third_party\blink\renderer\modules\webgl\webgl_rendering_context_base.cc" `
  -OldText @'
  // UXR: real-NVIDIA-desktop precision when persona active (float 127/127/23,
  // int 31/30/0). Detectors hash getShaderPrecisionFormat alongside the GPU string.
  if (base::UxrConfig::GetInstance().Has("uxr-webgl-fullparams") ||
      base::UxrConfig::GetInstance().Has("uxr-webgl-renderer")) {
    const bool p_is_float = precision_type == GL_LOW_FLOAT ||
                            precision_type == GL_MEDIUM_FLOAT ||
                            precision_type == GL_HIGH_FLOAT;
    return MakeGarbageCollected<WebGLShaderPrecisionFormat>(
        p_is_float ? 127 : 31, p_is_float ? 127 : 30, p_is_float ? 23 : 0);
  }
'@ `
  -NewText ""

Set-SourceReplacement `
  -RelativePath "third_party\blink\renderer\modules\webgl\webgl_rendering_context_base.cc" `
  -OldText @'
    if (format == GL_RGBA && type == GL_UNSIGNED_BYTE &&
        bridge_canvas_id_ != 0 && !bridge_webgl_unsupported_) {
      if (auto* bridge = canvas_bridge::CanvasBridgeClient::Get();
          bridge && bridge->Connected()) {
        auto remote = bridge->GetImageDataCacheFirst(
            bridge_canvas_id_, x, y, static_cast<uint32_t>(width),
            static_cast<uint32_t>(height));
        if (remote &&
            remote->size() == static_cast<size_t>(width) * height * 4) {
          std::memcpy(data, remote->data(), remote->size());
          return;
        }
      }
    }
'@ `
  -NewText ""

Set-SourceReplacement `
  -RelativePath "third_party\blink\renderer\modules\webgl\webgl_rendering_context_base.cc" `
  -OldText "    UxrFarbleReadPixels(data, width, height, format, type);  // UXR" `
  -NewText ""

Set-SourceReplacement `
  -RelativePath "third_party\blink\renderer\modules\webgl\webgl_rendering_context_base.cc" `
  -OldText @'
  if (data && size > 0) {
    const uint8_t* bytes = static_cast<const uint8_t*>(data);
    RecordWebGLOp(21u, {static_cast<int32_t>(target),
                        static_cast<int32_t>(usage)}, {}, bridge_canvas_id_,
                  drawingBufferWidth(), drawingBufferHeight(), {},
                  std::vector<uint8_t>(bytes, bytes + size));
  } else {
    RecordWebGLOp(22u, {static_cast<int32_t>(target),
                        static_cast<int32_t>(size),
                        static_cast<int32_t>(usage)}, {}, bridge_canvas_id_,
                  drawingBufferWidth(), drawingBufferHeight());
  }
'@ `
  -NewText @'
  if (auto* bridge = canvas_bridge::CanvasBridgeClient::Get();
      bridge && bridge->Connected()) {
    if (data && size > 0) {
      const uint8_t* bytes = static_cast<const uint8_t*>(data);
      RecordWebGLOp(21u, {static_cast<int32_t>(target),
                          static_cast<int32_t>(usage)}, {}, bridge_canvas_id_,
                    drawingBufferWidth(), drawingBufferHeight(), {},
                    std::vector<uint8_t>(bytes, bytes + size));
    } else {
      RecordWebGLOp(22u, {static_cast<int32_t>(target),
                          static_cast<int32_t>(size),
                          static_cast<int32_t>(usage)}, {}, bridge_canvas_id_,
                    drawingBufferWidth(), drawingBufferHeight());
    }
  }
'@

Set-SourceReplacement `
  -RelativePath "third_party\blink\renderer\modules\webgl\webgl2_rendering_context_base.cc" `
  -OldText '#include "base/uxr_config.h"' `
  -NewText ""

Set-SourceReplacement `
  -RelativePath "third_party\blink\renderer\modules\webgl\webgl2_rendering_context_base.cc" `
  -OldText @'
      // Normalize GLSL/VERSION so WebGL2 matches the spoofed renderer.
      if (base::UxrConfig::GetInstance().Has("uxr-webgl-fullparams") ||
          base::UxrConfig::GetInstance().Has("uxr-webgl-renderer")) {
        return WebGLAny(
            script_state,
            String("WebGL GLSL ES 3.00 (OpenGL ES GLSL ES 3.0 Chromium)"));
      }
'@ `
  -NewText @'
      // Keep WebGL 2 version strings derived from the active backend.
'@ `
  -CurrentMarker 'String("WebGL GLSL ES 3.00 (OpenGL ES GLSL ES 3.0 Chromium)")'

Set-SourceReplacement `
  -RelativePath "third_party\blink\renderer\modules\webgl\webgl2_rendering_context_base.cc" `
  -OldText @'
      if (base::UxrConfig::GetInstance().Has("uxr-webgl-fullparams") ||
          base::UxrConfig::GetInstance().Has("uxr-webgl-renderer")) {
        return WebGLAny(script_state,
                        String("WebGL 2.0 (OpenGL ES 3.0 Chromium)"));
      }
'@ `
  -NewText ""

Set-SourceReplacement `
  -RelativePath "third_party\blink\renderer\core\html\canvas\text_metrics.cc" `
  -OldText '#include <cmath>' `
  -NewText ""

Set-SourceReplacement `
  -RelativePath "third_party\blink\renderer\core\html\canvas\text_metrics.cc" `
  -OldText '#include "base/bit_cast.h"' `
  -NewText ""

Set-SourceReplacement `
  -RelativePath "third_party\blink\renderer\core\html\canvas\text_metrics.cc" `
  -OldText '#include "base/strings/string_number_conversions.h"  // UXR' `
  -NewText ""

Set-SourceReplacement `
  -RelativePath "third_party\blink\renderer\core\html\canvas\text_metrics.cc" `
  -OldText '#include "base/uxr_config.h"                          // UXR' `
  -NewText ""

Set-SourceReplacement `
  -RelativePath "third_party\blink\renderer\core\html\canvas\text_metrics.cc" `
  -OldText @'
// UXR: seeded, value-dependent sub-pixel jitter for TextMetrics. Canvas/audio/DOMRect
// are already noised; measureText() exposed a stable, substituted-font measurement fingerprint.
// Keyed on the canvas seed so it is per-persona and stable within a session. Fail-closed:
// no/zero/invalid seed or non-finite/zero value -> returned unchanged.
static double UxrJitterMetric(double v, uint32_t seed, uint32_t salt) {
  if (seed == 0u || v == 0.0 || !std::isfinite(v))
    return v;
  uint64_t bits = base::bit_cast<uint64_t>(v);
  uint32_t z = seed ^ salt ^ static_cast<uint32_t>(bits) ^
               static_cast<uint32_t>(bits >> 32);
  z ^= z >> 16; z *= 0x85ebca6bu; z ^= z >> 13; z *= 0xc2b2ae35u; z ^= z >> 16;
  double delta = (static_cast<double>(z & 0xffffu) / 65535.0 - 0.5) * 0.0125;
  return v + delta;
}

'@ `
  -NewText ""

Set-SourceReplacement `
  -RelativePath "third_party\blink\renderer\core\html\canvas\text_metrics.cc" `
  -OldText 'void TextMetrics::Update(const Font* font,' `
  -NewText @'
// Text metrics remain derived from the actual shaped and rendered font.
void TextMetrics::Update(const Font* font,
'@

Set-SourceReplacement `
  -RelativePath "third_party\blink\renderer\core\html\canvas\text_metrics.cc" `
  -OldText @'
  // UXR: apply per-persona jitter so TextMetrics is not a stable measurement fingerprint.
  uint32_t ph_seed = 0;
  base::UxrConfig& ph_cfg = base::UxrConfig::GetInstance();
  if (ph_cfg.Has("uxr-canvas-seed"))
    base::StringToUint(ph_cfg.Get("uxr-canvas-seed"), &ph_seed);
  if (ph_seed != 0u) {
    width_ = UxrJitterMetric(width_, ph_seed, 0x1001u);
    actual_bounding_box_left_ = UxrJitterMetric(actual_bounding_box_left_, ph_seed, 0x1002u);
    actual_bounding_box_right_ = UxrJitterMetric(actual_bounding_box_right_, ph_seed, 0x1003u);
    actual_bounding_box_ascent_ = UxrJitterMetric(actual_bounding_box_ascent_, ph_seed, 0x1004u);
    actual_bounding_box_descent_ = UxrJitterMetric(actual_bounding_box_descent_, ph_seed, 0x1005u);
    font_bounding_box_ascent_ = UxrJitterMetric(font_bounding_box_ascent_, ph_seed, 0x1006u);
    font_bounding_box_descent_ = UxrJitterMetric(font_bounding_box_descent_, ph_seed, 0x1007u);
    em_height_ascent_ = UxrJitterMetric(em_height_ascent_, ph_seed, 0x1008u);
    em_height_descent_ = UxrJitterMetric(em_height_descent_, ph_seed, 0x1009u);
  }
'@ `
  -NewText ""

Set-SourceReplacement `
  -RelativePath "third_party\blink\renderer\platform\fonts\font_cache.cc" `
  -OldText '#include "base/uxr_config.h"  // UXR' `
  -NewText @'
#include "base/strings/string_split.h"
#include "base/strings/string_util.h"
#include "base/uxr_config.h"
'@

Set-SourceReplacement `
  -RelativePath "third_party\blink\renderer\platform\fonts\font_cache.cc" `
  -OldText @'
namespace {
// UXR: persona font availability. Font-enumeration fingerprinting measures
// per-family glyph metrics to list the fonts the host actually has; a Linux
// host resolving "Ubuntu"/"Noto Color Emoji" contradicts a Windows persona.
// When uxr-font-whitelist is set (comma-separated, case-insensitive family
// names), every non-generic family not on the list resolves to nullptr, so CSS
// falls through to the next family exactly as on a machine without it.
// Unset -> native font resolution untouched.
bool UxrFontHidden(const AtomicString& family) {
  if (family.empty()) {
    return false;
  }
  static const char* const kGenerics[] = {
      "serif",         "sans-serif",       "monospace",  "cursive",
      "fantasy",       "system-ui",        "math",       "emoji",
      "fangsong",      "ui-serif",         "ui-sans-serif", "ui-monospace",
      "ui-rounded",    "ui-fangsong",      "-webkit-body",
      "-webkit-pictograph", "-webkit-system-font", "-webkit-control"};
  std::string ph_name = family.GetString().ToAsciiLower().Utf8();
  for (const char* ph_g : kGenerics) {
    if (ph_name == ph_g) {
      return false;
    }
  }
  const std::string ph_list =
      base::UxrConfig::GetInstance().Get("uxr-font-whitelist");
  if (ph_list.empty()) {
    return false;
  }
  // Substring match on comma-separated entries, both sides lowercased and
  // space-trimmed. A quoted family with commas would break this — acceptable:
  // persona generators emit plain family names.
  std::string ph_norm;
  for (char ph_c : ph_list) {
    if (ph_c == ' ' || ph_c == '\t') {
      continue;
    }
    ph_norm += (ph_c >= 'A' && ph_c <= 'Z')
                   ? static_cast<char>(ph_c - 'A' + 'a')
                   : ph_c;
  }
  size_t ph_pos = 0;
  while (ph_pos < ph_norm.size()) {
    const size_t ph_comma = ph_norm.find(',', ph_pos);
    const size_t ph_end =
        ph_comma == std::string::npos ? ph_norm.size() : ph_comma;
    if (ph_name == ph_norm.substr(ph_pos, ph_end - ph_pos)) {
      return false;  // whitelisted: available
    }
    ph_pos = ph_comma == std::string::npos ? ph_norm.size() : ph_comma + 1;
  }
  return true;
}
}  // namespace

'@ `
  -NewText @'
namespace {

bool UxrFontFamilyIsGeneric(const AtomicString& family) {
  const std::string name = family.GetString().ToAsciiLower().Utf8();
  static constexpr const char* kGenericFamilies[] = {
      "serif",       "sans-serif", "monospace",    "cursive",
      "fantasy",     "system-ui",  "math",         "emoji",
      "fangsong",    "ui-serif",   "ui-sans-serif", "ui-monospace",
      "ui-rounded",  "ui-fangsong", "-webkit-body",
      "-webkit-pictograph", "-webkit-system-font", "-webkit-control"};
  for (const char* generic : kGenericFamilies) {
    if (name == generic)
      return true;
  }
  return false;
}

bool UxrFontFamilyAllowed(const AtomicString& family) {
  if (family.empty() || UxrFontFamilyIsGeneric(family))
    return true;

  const std::string whitelist =
      base::UxrConfig::GetInstance().Get("uxr-font-whitelist");
  if (whitelist.empty())
    return true;

  const std::string requested = family.GetString().Utf8();
  for (const std::string& entry : base::SplitString(
           whitelist, ",", base::TRIM_WHITESPACE, base::SPLIT_WANT_NONEMPTY)) {
    if (base::EqualsCaseInsensitiveASCII(entry, requested))
      return true;
  }
  return false;
}

}  // namespace

'@

Set-SourceReplacement `
  -RelativePath "third_party\blink\renderer\platform\fonts\font_cache.cc" `
  -OldText @'
namespace {

bool UxrFontFamilyIsGeneric(const AtomicString& family) {
  const std::string name = family.GetString().ToAsciiLower().Utf8();
  static constexpr const char* kGenericFamilies[] = {
      "serif",       "sans-serif", "monospace",    "cursive",
      "fantasy",     "system-ui",  "math",         "emoji",
      "fangsong",    "ui-serif",   "ui-sans-serif", "ui-monospace",
      "ui-rounded",  "ui-fangsong", "-webkit-body",
      "-webkit-pictograph", "-webkit-system-font", "-webkit-control"};
  for (const char* generic : kGenericFamilies) {
    if (name == generic)
      return true;
  }
  return false;
}

bool UxrFontFamilyAllowed(const AtomicString& family) {
  if (family.empty() || UxrFontFamilyIsGeneric(family))
    return true;

  const std::string whitelist =
      base::UxrConfig::GetInstance().Get("uxr-font-whitelist");
  if (whitelist.empty())
    return true;

  const std::string requested = family.GetString().Utf8();
  for (const std::string& entry : base::SplitString(
           whitelist, ",", base::TRIM_WHITESPACE, base::SPLIT_WANT_NONEMPTY)) {
    if (base::EqualsCaseInsensitiveASCII(entry, requested))
      return true;
  }
  return false;
}

}  // namespace

'@ `
  -NewText @'
namespace {

bool UxrFontFamilyIsGeneric(const AtomicString& family) {
  const std::string name = family.GetString().ToAsciiLower().Utf8();
  static constexpr const char* kGenericFamilies[] = {
      "serif",       "sans-serif", "monospace",    "cursive",
      "fantasy",     "system-ui",  "math",         "emoji",
      "fangsong",    "ui-serif",   "ui-sans-serif", "ui-monospace",
      "ui-rounded",  "ui-fangsong", "-webkit-body",
      "-webkit-pictograph", "-webkit-system-font", "-webkit-control"};
  for (const char* generic : kGenericFamilies) {
    if (name == generic)
      return true;
  }
  return false;
}

bool UxrFontFamilyAllowed(const AtomicString& family) {
  if (family.empty() || UxrFontFamilyIsGeneric(family))
    return true;

  const std::string requested = family.GetString().Utf8();
  const std::string whitelist =
      base::UxrConfig::GetInstance().Get("uxr-font-whitelist");
  if (!whitelist.empty()) {
    for (const std::string& entry : base::SplitString(
             whitelist, ",", base::TRIM_WHITESPACE,
             base::SPLIT_WANT_NONEMPTY)) {
      if (base::EqualsCaseInsensitiveASCII(entry, requested))
        return true;
    }
    return false;
  }

  const std::string persona =
      base::UxrConfig::GetInstance().Get("uxr-platform");
  if (!base::EqualsCaseInsensitiveASCII(persona, "windows") &&
      !base::EqualsCaseInsensitiveASCII(persona, "win32")) {
    return true;
  }

  // Clearcote-style canonical Windows family set. This prevents unusual
  // user-installed host fonts from leaking when no imported list is supplied.
  static constexpr const char* kWindowsFamilies[] = {
      "Arial", "Arial Black", "Bahnschrift", "Calibri", "Cambria",
      "Cambria Math", "Candara", "Cascadia Code", "Cascadia Mono",
      "Comic Sans MS", "Consolas", "Constantia", "Corbel", "Courier New",
      "Ebrima", "Franklin Gothic Medium", "Gabriola", "Gadugi", "Georgia",
      "HoloLens MDL2 Assets", "Impact", "Ink Free", "Javanese Text",
      "Leelawadee UI", "Lucida Console", "Lucida Sans Unicode",
      "Malgun Gothic", "Marlett", "Microsoft Himalaya",
      "Microsoft JhengHei", "Microsoft New Tai Lue", "Microsoft PhagsPa",
      "Microsoft Sans Serif", "Microsoft Tai Le", "Microsoft YaHei",
      "Microsoft Yi Baiti", "MingLiU-ExtB", "Mongolian Baiti", "MS Gothic",
      "MV Boli", "Myanmar Text", "Nirmala UI", "Palatino Linotype",
      "Segoe Fluent Icons", "Segoe MDL2 Assets", "Segoe Print",
      "Segoe Script", "Segoe UI", "Segoe UI Emoji", "Segoe UI Historic",
      "Segoe UI Symbol", "Segoe UI Variable", "SimSun", "Sitka", "Sylfaen",
      "Symbol", "Tahoma", "Times New Roman", "Trebuchet MS", "Verdana",
      "Webdings", "Wingdings", "Yu Gothic"};
  for (const char* allowed : kWindowsFamilies) {
    if (base::EqualsCaseInsensitiveASCII(allowed, requested))
      return true;
  }
  return false;
}

}  // namespace

'@

Set-SourceReplacement `
  -RelativePath "third_party\blink\renderer\platform\fonts\font_cache.cc" `
  -OldText @'
  if (UxrFontHidden(family)) {  // UXR: persona font availability
    return nullptr;
  }
'@ `
  -NewText ""

Set-SourceReplacement `
  -RelativePath "third_party\blink\renderer\platform\fonts\font_cache.cc" `
  -OldText '  TRACE_EVENT0("fonts", "FontCache::GetFontPlatformData");' `
  -NewText @'
  // Restrict only native family lookup. Downloaded faces, unique-name lookup,
  // and last-resort fallback retain normal Blink behavior.
  if (creation_params.CreationType() == kCreateFontByFamily &&
      alternate_font_name != AlternateFontName::kLocalUniqueFace &&
      alternate_font_name != AlternateFontName::kLastResort &&
      !UxrFontFamilyAllowed(creation_params.Family())) {
    return nullptr;
  }

  TRACE_EVENT0("fonts", "FontCache::GetFontPlatformData");
'@

Set-SourceReplacement `
  -RelativePath "third_party\blink\renderer\modules\webgl\webgl_rendering_context_base.cc" `
  -OldText @'
#include <inttypes.h>

#include <memory>
'@ `
  -NewText @'
#include <inttypes.h>

#include <array>
#include <memory>
'@

Set-SourceReplacement `
  -RelativePath "third_party\blink\renderer\modules\webgl\webgl_rendering_context_base.cc" `
  -OldText @'
namespace {

enum class WebGLANGLEImplementation {
'@ `
  -NewText @'
namespace {

constexpr char kDefaultWebGLVendor[] = "Google Inc. (Intel)";
constexpr char kDefaultWebGLRenderer[] =
    "ANGLE (Intel, Intel(R) UHD Graphics 770 (0x0000A780) Direct3D11 "
    "vs_5_0 ps_5_0, D3D11)";

const std::string& WebGLPersonaVendor() {
  static const std::string vendor = [] {
    const auto& config = base::UxrConfig::GetInstance();
    const std::string configured_vendor = config.Get("uxr-webgl-vendor");
    const std::string configured_renderer = config.Get("uxr-webgl-renderer");
    return !configured_vendor.empty() && !configured_renderer.empty()
               ? configured_vendor
               : std::string(kDefaultWebGLVendor);
  }();
  return vendor;
}

const std::string& WebGLPersonaRenderer() {
  static const std::string renderer = [] {
    const auto& config = base::UxrConfig::GetInstance();
    const std::string configured_vendor = config.Get("uxr-webgl-vendor");
    const std::string configured_renderer = config.Get("uxr-webgl-renderer");
    return !configured_vendor.empty() && !configured_renderer.empty()
               ? configured_renderer
               : std::string(kDefaultWebGLRenderer);
  }();
  return renderer;
}

GLint ClampWebGLPersonaLimit(gpu::gles2::GLES2Interface* gl,
                             GLenum pname,
                             GLint persona_value) {
  GLint real_value = 0;
  gl->GetIntegerv(pname, &real_value);
  return real_value > 0 && real_value < persona_value ? real_value
                                                       : persona_value;
}

std::array<GLint, 2> ClampWebGLPersonaViewport(
    gpu::gles2::GLES2Interface* gl,
    GLint persona_value) {
  std::array<GLint, 2> real_values = {0, 0};
  gl->GetIntegerv(GL_MAX_VIEWPORT_DIMS, real_values.data());
  for (GLint& value : real_values) {
    if (value <= 0 || value > persona_value)
      value = persona_value;
  }
  return real_values;
}

enum class WebGLANGLEImplementation {
'@

Set-SourceReplacement `
  -RelativePath "third_party\blink\renderer\modules\webgl\webgl_rendering_context_base.cc" `
  -OldText @'
    case GL_MAX_COMBINED_TEXTURE_IMAGE_UNITS:
      return GetIntParameter(script_state, pname);
    case GL_MAX_CUBE_MAP_TEXTURE_SIZE:
      return GetIntParameter(script_state, pname);
    case GL_MAX_FRAGMENT_UNIFORM_VECTORS:
      return GetIntParameter(script_state, pname);
    case GL_MAX_RENDERBUFFER_SIZE:
      return GetIntParameter(script_state, pname);
    case GL_MAX_TEXTURE_IMAGE_UNITS:
      return GetIntParameter(script_state, pname);
    case GL_MAX_TEXTURE_SIZE:
      return GetIntParameter(script_state, pname);
    case GL_MAX_VARYING_VECTORS:
      return GetIntParameter(script_state, pname);
    case GL_MAX_VERTEX_ATTRIBS:
      return GetIntParameter(script_state, pname);
    case GL_MAX_VERTEX_TEXTURE_IMAGE_UNITS:
      return GetIntParameter(script_state, pname);
    case GL_MAX_VERTEX_UNIFORM_VECTORS:
      return GetIntParameter(script_state, pname);
    case GL_MAX_VIEWPORT_DIMS:
      return GetWebGLIntArrayParameter(script_state, pname);
'@ `
  -NewText @'
    case GL_MAX_COMBINED_TEXTURE_IMAGE_UNITS:
      return WebGLAny(script_state, ClampWebGLPersonaLimit(
                                        ContextGL(), pname, 32));
    case GL_MAX_CUBE_MAP_TEXTURE_SIZE:
      return WebGLAny(script_state, ClampWebGLPersonaLimit(
                                        ContextGL(), pname, 16384));
    case GL_MAX_FRAGMENT_UNIFORM_VECTORS:
      return WebGLAny(script_state, ClampWebGLPersonaLimit(
                                        ContextGL(), pname, 1024));
    case GL_MAX_RENDERBUFFER_SIZE:
      return WebGLAny(script_state, ClampWebGLPersonaLimit(
                                        ContextGL(), pname, 16384));
    case GL_MAX_TEXTURE_IMAGE_UNITS:
      return WebGLAny(script_state, ClampWebGLPersonaLimit(
                                        ContextGL(), pname, 16));
    case GL_MAX_TEXTURE_SIZE:
      return WebGLAny(script_state, ClampWebGLPersonaLimit(
                                        ContextGL(), pname, 16384));
    case GL_MAX_VARYING_VECTORS:
      return WebGLAny(script_state, ClampWebGLPersonaLimit(
                                        ContextGL(), pname, 30));
    case GL_MAX_VERTEX_ATTRIBS:
      return WebGLAny(script_state, ClampWebGLPersonaLimit(
                                        ContextGL(), pname, 16));
    case GL_MAX_VERTEX_TEXTURE_IMAGE_UNITS:
      return WebGLAny(script_state, ClampWebGLPersonaLimit(
                                        ContextGL(), pname, 16));
    case GL_MAX_VERTEX_UNIFORM_VECTORS:
      return WebGLAny(script_state, ClampWebGLPersonaLimit(
                                        ContextGL(), pname, 4096));
    case GL_MAX_VIEWPORT_DIMS: {
      const auto viewport = ClampWebGLPersonaViewport(ContextGL(), 32767);
      return WebGLAny(script_state,
                      DOMInt32Array::Create(base::span(viewport)));
    }
'@

Set-SourceReplacement `
  -RelativePath "third_party\blink\renderer\modules\webgl\webgl_rendering_context_base.cc" `
  -OldText @'
    case GL_SHADING_LANGUAGE_VERSION:
      return WebGLAny(
          script_state,
          StrCat({"WebGL GLSL ES 1.0 (",
                  String(ContextGL()->GetString(GL_SHADING_LANGUAGE_VERSION)),
                  ")"}));
'@ `
  -NewText @'
    case GL_SHADING_LANGUAGE_VERSION:
      return WebGLAny(
          script_state,
          String("WebGL GLSL ES 1.0 (OpenGL ES GLSL ES 1.0 Chromium)"));
'@

Set-SourceReplacement `
  -RelativePath "third_party\blink\renderer\modules\webgl\webgl_rendering_context_base.cc" `
  -OldText @'
    case GL_VERSION:
      return WebGLAny(
          script_state,
          StrCat({"WebGL 1.0 (", String(ContextGL()->GetString(GL_VERSION)),
                  ")"}));
'@ `
  -NewText @'
    case GL_VERSION:
      return WebGLAny(script_state,
                      String("WebGL 1.0 (OpenGL ES 2.0 Chromium)"));
'@

Set-SourceReplacement `
  -RelativePath "third_party\blink\renderer\modules\webgl\webgl_rendering_context_base.cc" `
  -OldText @'
        const auto& config = base::UxrConfig::GetInstance();
        const std::string renderer = config.Get("uxr-webgl-renderer");
        const std::string vendor = config.Get("uxr-webgl-vendor");
        if (!renderer.empty() && !vendor.empty()) {
          return WebGLAny(script_state, String(renderer.c_str()));
        }
        if (base::FeatureList::IsEnabled(blink::features::kSpoofWebGLInfo))
          return WebGLAny(script_state, String(blink::features::kSpoofWebGLRendererParam.Get()));
        return WebGLAny(script_state,
                        String(ContextGL()->GetString(GL_RENDERER)));
'@ `
  -NewText @'
        return WebGLAny(script_state,
                        String(WebGLPersonaRenderer().c_str()));
'@

Set-SourceReplacement `
  -RelativePath "third_party\blink\renderer\modules\webgl\webgl_rendering_context_base.cc" `
  -OldText @'
        const auto& config = base::UxrConfig::GetInstance();
        const std::string renderer = config.Get("uxr-webgl-renderer");
        const std::string vendor = config.Get("uxr-webgl-vendor");
        if (!renderer.empty() && !vendor.empty()) {
          return WebGLAny(script_state, String(vendor.c_str()));
        }
        if (base::FeatureList::IsEnabled(blink::features::kSpoofWebGLInfo))
          return WebGLAny(script_state, String(blink::features::kSpoofWebGLVendorParam.Get()));
        return WebGLAny(script_state,
                        String(ContextGL()->GetString(GL_VENDOR)));
'@ `
  -NewText @'
        return WebGLAny(script_state,
                        String(WebGLPersonaVendor().c_str()));
'@

Set-SourceReplacement `
  -RelativePath "third_party\blink\renderer\modules\webgl\webgl2_rendering_context_base.cc" `
  -OldText 'const GLuint64 kMaxClientWaitTimeout = 0u;' `
  -NewText @'
const GLuint64 kMaxClientWaitTimeout = 0u;

GLint ClampWebGL2PersonaLimit(gpu::gles2::GLES2Interface* gl,
                              GLenum pname,
                              GLint persona_value) {
  GLint real_value = 0;
  gl->GetIntegerv(pname, &real_value);
  return real_value > 0 && real_value < persona_value ? real_value
                                                       : persona_value;
}

GLint WebGL2PersonaVaryingVectors(gpu::gles2::GLES2Interface* gl) {
  return ClampWebGL2PersonaLimit(gl, GL_MAX_VARYING_VECTORS, 30);
}
'@ `
  -CurrentMarker @(
    'GLint ClampWebGL2PersonaLimit',
    'GLint ClampPersonaLimit'
  ) `
  -PreferCurrentMarker

Set-SourceReplacement `
  -RelativePath "third_party\blink\renderer\modules\webgl\webgl2_rendering_context_base.cc" `
  -OldText @'
    case GL_SHADING_LANGUAGE_VERSION: {
      // Keep WebGL 2 version strings derived from the active backend.
      return WebGLAny(
          script_state,
          StrCat({"WebGL GLSL ES 3.00 (",
                  String(ContextGL()->GetString(GL_SHADING_LANGUAGE_VERSION)),
                  ")"}));
    }
    case GL_VERSION:
      return WebGLAny(
          script_state,
          StrCat({"WebGL 2.0 (", String(ContextGL()->GetString(GL_VERSION)),
                  ")"}));
'@ `
  -NewText @'
    case GL_SHADING_LANGUAGE_VERSION:
      return WebGLAny(
          script_state,
          String("WebGL GLSL ES 3.00 (OpenGL ES GLSL ES 3.0 Chromium)"));
    case GL_VERSION:
      return WebGLAny(script_state,
                      String("WebGL 2.0 (OpenGL ES 3.0 Chromium)"));
'@

Set-SourceReplacement `
  -RelativePath "third_party\blink\renderer\modules\webgl\webgl2_rendering_context_base.cc" `
  -OldText @'
    case GL_MAX_3D_TEXTURE_SIZE:
      return GetIntParameter(script_state, pname);
    case GL_MAX_ARRAY_TEXTURE_LAYERS:
      return GetIntParameter(script_state, pname);
'@ `
  -NewText @'
    case GL_MAX_3D_TEXTURE_SIZE:
      return WebGLAny(script_state, ClampWebGL2PersonaLimit(
                                        ContextGL(), pname, 2048));
    case GL_MAX_ARRAY_TEXTURE_LAYERS:
      return WebGLAny(script_state, ClampWebGL2PersonaLimit(
                                        ContextGL(), pname, 2048));
'@

Set-SourceReplacement `
  -RelativePath "third_party\blink\renderer\modules\webgl\webgl2_rendering_context_base.cc" `
  -OldText @'
    case GL_MAX_COLOR_ATTACHMENTS:
      return GetIntParameter(script_state, pname);
'@ `
  -NewText @'
    case GL_MAX_COLOR_ATTACHMENTS:
      return WebGLAny(script_state, ClampWebGL2PersonaLimit(
                                        ContextGL(), pname, 8));
'@

Set-SourceReplacement `
  -RelativePath "third_party\blink\renderer\modules\webgl\webgl2_rendering_context_base.cc" `
  -OldText @'
    case GL_MAX_COMBINED_UNIFORM_BLOCKS:
      return GetIntParameter(script_state, pname);
'@ `
  -NewText @'
    case GL_MAX_COMBINED_UNIFORM_BLOCKS:
      return WebGLAny(script_state, ClampWebGL2PersonaLimit(
                                        ContextGL(), pname, 72));
'@

Set-SourceReplacement `
  -RelativePath "third_party\blink\renderer\modules\webgl\webgl2_rendering_context_base.cc" `
  -OldText @'
    case GL_MAX_DRAW_BUFFERS:
      return GetIntParameter(script_state, pname);
'@ `
  -NewText @'
    case GL_MAX_DRAW_BUFFERS:
      return WebGLAny(script_state, ClampWebGL2PersonaLimit(
                                        ContextGL(), pname, 8));
'@

Set-SourceReplacement `
  -RelativePath "third_party\blink\renderer\modules\webgl\webgl2_rendering_context_base.cc" `
  -OldText @'
    case GL_MAX_FRAGMENT_INPUT_COMPONENTS:
      return GetIntParameter(script_state, pname);
'@ `
  -NewText @'
    case GL_MAX_FRAGMENT_INPUT_COMPONENTS:
      return WebGLAny(script_state,
                      4 * WebGL2PersonaVaryingVectors(ContextGL()));
'@

Set-SourceReplacement `
  -RelativePath "third_party\blink\renderer\modules\webgl\webgl2_rendering_context_base.cc" `
  -OldText @'
    case GL_MAX_FRAGMENT_UNIFORM_BLOCKS:
      return GetIntParameter(script_state, pname);
'@ `
  -NewText @'
    case GL_MAX_FRAGMENT_UNIFORM_BLOCKS:
      return WebGLAny(script_state, ClampWebGL2PersonaLimit(
                                        ContextGL(), pname, 12));
'@

Set-SourceReplacement `
  -RelativePath "third_party\blink\renderer\modules\webgl\webgl2_rendering_context_base.cc" `
  -OldText @'
    case GL_MAX_FRAGMENT_UNIFORM_COMPONENTS:
      return GetIntParameter(script_state, pname);
'@ `
  -NewText @'
    case GL_MAX_FRAGMENT_UNIFORM_COMPONENTS:
      return WebGLAny(script_state, ClampWebGL2PersonaLimit(
                                        ContextGL(), pname, 4096));
'@

Set-SourceReplacement `
  -RelativePath "third_party\blink\renderer\modules\webgl\webgl2_rendering_context_base.cc" `
  -OldText @'
    case GL_MAX_SAMPLES:
      return GetIntParameter(script_state, pname);
'@ `
  -NewText @'
    case GL_MAX_SAMPLES:
      return WebGLAny(script_state, ClampWebGL2PersonaLimit(
                                        ContextGL(), pname, 4));
'@

Set-SourceReplacement `
  -RelativePath "third_party\blink\renderer\modules\webgl\webgl2_rendering_context_base.cc" `
  -OldText @'
    case GL_MAX_UNIFORM_BUFFER_BINDINGS:
      return GetIntParameter(script_state, pname);
'@ `
  -NewText @'
    case GL_MAX_UNIFORM_BUFFER_BINDINGS:
      return WebGLAny(script_state, ClampWebGL2PersonaLimit(
                                        ContextGL(), pname, 72));
'@

Set-SourceReplacement `
  -RelativePath "third_party\blink\renderer\modules\webgl\webgl2_rendering_context_base.cc" `
  -OldText @'
    case GL_MAX_VARYING_COMPONENTS:
      return GetIntParameter(script_state, pname);
    case GL_MAX_VERTEX_OUTPUT_COMPONENTS:
      return GetIntParameter(script_state, pname);
'@ `
  -NewText @'
    case GL_MAX_VARYING_COMPONENTS:
    case GL_MAX_VERTEX_OUTPUT_COMPONENTS:
      return WebGLAny(script_state,
                      4 * WebGL2PersonaVaryingVectors(ContextGL()));
'@

Set-SourceReplacement `
  -RelativePath "third_party\blink\renderer\modules\webgl\webgl2_rendering_context_base.cc" `
  -OldText @'
    case GL_MAX_VERTEX_UNIFORM_BLOCKS:
      return GetIntParameter(script_state, pname);
'@ `
  -NewText @'
    case GL_MAX_VERTEX_UNIFORM_BLOCKS:
      return WebGLAny(script_state, ClampWebGL2PersonaLimit(
                                        ContextGL(), pname, 12));
'@

Set-SourceReplacement `
  -RelativePath "third_party\blink\renderer\modules\webgl\webgl2_rendering_context_base.cc" `
  -OldText @'
    case GL_MAX_VERTEX_UNIFORM_COMPONENTS:
      return GetIntParameter(script_state, pname);
'@ `
  -NewText @'
    case GL_MAX_VERTEX_UNIFORM_COMPONENTS:
      return WebGLAny(script_state, ClampWebGL2PersonaLimit(
                                        ContextGL(), pname, 16384));
'@

function Normalize-RestoredSource {
  param(
    [Parameter(Mandatory)] [string]$RelativePath,
    [Parameter(Mandatory)] [scriptblock]$Transform
  )
  $path = Join-Path $Src $RelativePath
  if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
    throw "resume source file is missing: $RelativePath"
  }
  $content = [IO.File]::ReadAllText($path)
  $updated = & $Transform $content
  if ($updated -ne $content) {
    [IO.File]::WriteAllText($path, $updated)
    Write-Host "==> normalized restored source: $RelativePath"
  }
}

Normalize-RestoredSource `
  -RelativePath "third_party\blink\renderer\modules\webgl\webgl2_rendering_context_base.cc" `
  -Transform {
    param($content)
    $content = [regex]::Replace(
        $content,
        '(?ms)\r?\n\s*// Normalize GLSL/VERSION so WebGL2 matches the spoofed renderer\..*?\r?\n\s*\}',
        "")
    $content = [regex]::Replace(
        $content,
        '(?ms)\r?\n\s*if \(base::UxrConfig::GetInstance\(\)\.Has\("uxr-webgl-fullparams"\).*?String\("WebGL 2\.0 \(OpenGL ES 3\.0 Chromium\)"\)\);\s*\}',
        "")
    $legacyFunctions = @(
      @{ Name = "ClampWebGL2PersonaLimit"; ReturnType = "GLint" },
      @{ Name = "WebGL2PersonaVaryingVectors"; ReturnType = "GLint" }
    )
    $hasCurrentClamp = $content -match '(?m)^\s*GLint\s+ClampPersonaLimit\s*\('
    foreach ($legacyFunction in $legacyFunctions) {
      $signaturePattern = '(?m)^[ \t]*' +
          [regex]::Escape($legacyFunction.ReturnType) + '\s+' +
          [regex]::Escape($legacyFunction.Name) + '\s*\('
      $matches = [regex]::Matches($content, $signaturePattern)
      $keepCount = if ($hasCurrentClamp) { 0 } else { 1 }
      for ($index = $matches.Count - 1; $index -ge $keepCount; $index--) {
        $start = $matches[$index].Index
        $openBrace = $content.IndexOf('{', $start)
        if ($openBrace -lt 0) {
          continue
        }
        $depth = 0
        $end = -1
        for ($position = $openBrace; $position -lt $content.Length; $position++) {
          switch ($content[$position]) {
            '{' { $depth++ }
            '}' {
              $depth--
              if ($depth -eq 0) {
                $end = $position + 1
                break
              }
            }
          }
          if ($end -ge 0) {
            break
          }
        }
        if ($end -ge 0) {
          while ($end -lt $content.Length -and
                 ($content[$end] -eq "`r" -or $content[$end] -eq "`n")) {
            $end++
          }
          $content = $content.Remove($start, $end - $start)
        }
      }
    }
    return [regex]::Replace(
        $content,
        '(?m)^\s*#include "base/uxr_config\.h"\s*\r?\n',
        "")
  }

Normalize-RestoredSource `
  -RelativePath "third_party\blink\renderer\core\html\canvas\text_metrics.cc" `
  -Transform {
    param($content)
    $content = [regex]::Replace(
        $content,
        '(?ms)\r?\n\s*// UXR: seeded, value-dependent sub-pixel jitter for TextMetrics\..*?\r?\n\s*void TextMetrics::Update',
        "`r`nvoid TextMetrics::Update")
    $content = [regex]::Replace(
        $content,
        '(?ms)\r?\n\s*// UXR: apply per-persona jitter so TextMetrics is not a stable measurement fingerprint\..*?\r?\n\s*\}\s*\r?\n\}',
        "`r`n}")
    $content = [regex]::Replace($content, '(?m)^\s*#include <cmath>\s*\r?\n', "")
    $content = [regex]::Replace($content, '(?m)^\s*#include "base/bit_cast\.h"\s*\r?\n', "")
    $content = [regex]::Replace($content, '(?m)^\s*#include "base/strings/string_number_conversions\.h".*\r?\n', "")
    return [regex]::Replace($content, '(?m)^\s*#include "base/uxr_config\.h".*\r?\n', "")
  }

Normalize-RestoredSource `
  -RelativePath "third_party\blink\renderer\platform\fonts\font_cache.cc" `
  -Transform {
    param($content)
    $content = [regex]::Replace(
        $content,
        '(?ms)\r?\n\s*namespace \{\s*// UXR: persona font availability\..*?\r?\n\}\s*// namespace\s*\r?\n',
        "`r`n")
    $content = [regex]::Replace(
        $content,
        '(?ms)\r?\n\s*if \(UxrFontHidden\(family\).*?\r?\n\s*\}',
        "")
    if ($content -notmatch '#include "base/strings/string_split\.h"') {
      $include = @'
#include "base/strings/string_split.h"
#include "base/strings/string_util.h"
#include "base/uxr_config.h"
'@ + "`r`n"
      $content = $content.Replace('#include "base/timer/elapsed_timer.h"', $include + '#include "base/timer/elapsed_timer.h"')
    }
    if ($content -notmatch 'bool UxrFontFamilyAllowed\(') {
      $helper = @'

namespace {

bool UxrFontFamilyIsGeneric(const AtomicString& family) {
  const std::string name = family.GetString().ToAsciiLower().Utf8();
  static constexpr const char* kGenericFamilies[] = {
      "serif", "sans-serif", "monospace", "cursive", "fantasy",
      "system-ui", "math", "emoji", "fangsong", "ui-serif",
      "ui-sans-serif", "ui-monospace", "ui-rounded", "ui-fangsong",
      "-webkit-body", "-webkit-pictograph", "-webkit-system-font",
      "-webkit-control"};
  for (const char* generic : kGenericFamilies) {
    if (name == generic)
      return true;
  }
  return false;
}

bool UxrFontFamilyAllowed(const AtomicString& family) {
  if (family.empty() || UxrFontFamilyIsGeneric(family))
    return true;
  const std::string requested = family.GetString().Utf8();
  const std::string whitelist =
      base::UxrConfig::GetInstance().Get("uxr-font-whitelist");
  if (!whitelist.empty()) {
    for (const std::string& entry : base::SplitString(
             whitelist, ",", base::TRIM_WHITESPACE,
             base::SPLIT_WANT_NONEMPTY)) {
      if (base::EqualsCaseInsensitiveASCII(entry, requested))
        return true;
    }
    return false;
  }
  const std::string persona =
      base::UxrConfig::GetInstance().Get("uxr-platform");
  if (!base::EqualsCaseInsensitiveASCII(persona, "windows") &&
      !base::EqualsCaseInsensitiveASCII(persona, "win32"))
    return true;
  static constexpr const char* kWindowsFamilies[] = {
      "Arial", "Calibri", "Cambria", "Consolas", "Courier New", "Georgia",
      "Segoe UI", "Segoe UI Emoji", "Tahoma", "Times New Roman", "Verdana"};
  for (const char* allowed : kWindowsFamilies) {
    if (base::EqualsCaseInsensitiveASCII(allowed, requested))
      return true;
  }
  return false;
}

}  // namespace
'@
      $content = $content.Replace('namespace blink {', 'namespace blink {' + $helper)
    }
    return $content
  }

Normalize-RestoredSource `
  -RelativePath "third_party\blink\renderer\modules\webgl\webgl_rendering_context_base.cc" `
  -Transform {
    param($content)
    $content = [regex]::Replace(
        $content,
        '(?ms)String\(\s*(?:"ANGLE \(NVIDIA, NVIDIA GeForce RTX 3060 Direct3D11 vs_5_0\s*"\s*"ps_5_0, D3D11\)"|"ANGLE \(NVIDIA, NVIDIA GeForce RTX 3060 Direct3D11 vs_5_0 ps_5_0, D3D11\)")\s*\)',
        'String(WebGLPersonaRenderer().c_str())')
    $content = $content.Replace(
        'String("Google Inc. (NVIDIA Corporation)")',
        'String(WebGLPersonaVendor().c_str())')
    return $content
  }

$webglSources = @(
  "third_party\blink\renderer\modules\webgl\webgl_rendering_context_base.cc",
  "third_party\blink\renderer\modules\webgl\webgl2_rendering_context_base.cc"
)
$touchTime = [DateTime]::UtcNow.AddSeconds(2)
foreach ($relativePath in $webglSources) {
  $path = Join-Path $Src $relativePath
  if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
    throw "resume source file is missing: $relativePath"
  }
  [IO.File]::SetLastWriteTimeUtc($path, $touchTime)
  Write-Host "==> touched restored WebGL source: $relativePath"
}

$webglObjDir = Join-Path $OutDir "obj\third_party\blink\renderer\modules\webgl"
if (Test-Path -LiteralPath $webglObjDir -PathType Container) {
  $staleObjects = @(Get-ChildItem -LiteralPath $webglObjDir -Recurse -File -Include "*.obj","*.pch" -ErrorAction SilentlyContinue)
  if ($staleObjects.Count -gt 0) {
    $staleObjects | Remove-Item -Force
    Write-Host "==> removed $($staleObjects.Count) restored WebGL object files"
  }
}
$webglObjManifest = Join-Path $OutDir "chromix-webgl-objects-invalidated.txt"
"restored WebGL object files invalidated at $(Get-Date -Format o)" |
  Set-Content -Encoding ASCII $webglObjManifest
