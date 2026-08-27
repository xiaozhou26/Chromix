[CmdletBinding()]
param(
  [Parameter(Mandatory)] [string]$Src
)
$ErrorActionPreference = "Stop"

function Set-SourceReplacement {
  param(
    [Parameter(Mandatory)] [string]$RelativePath,
    [Parameter(Mandatory)] [string]$OldText,
    [Parameter(Mandatory)] [AllowEmptyString()] [string]$NewText
  )
  $path = Join-Path $Src $RelativePath
  if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
    throw "resume source file is missing: $RelativePath"
  }
  $content = [IO.File]::ReadAllText($path)
  if ($content.Contains($OldText)) {
    [IO.File]::WriteAllText($path, $content.Replace($OldText, $NewText))
    Write-Host "==> updated restored source: $RelativePath"
    return
  }
  if ($NewText -eq "") {
    Write-Host "==> restored source already current: $RelativePath"
    return
  }
  if (-not $content.Contains($NewText)) {
    throw "restored source does not contain the expected old or new text: $RelativePath"
  }
  Write-Host "==> restored source already current: $RelativePath"
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
'@

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
'@

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
'@

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
