import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[2]
WEBGL1 = REPO / "patches" / "0029-third_party-blink-renderer-modules-webgl-webgl_rendering_context_base-cc.patch"
WEBGL2 = REPO / "patches" / "0035-webgl2-getparameter-version-normalize.patch"
BRIDGE = REPO / "patches" / "0082-third_party-blink-renderer-modules-webgl-webgl_rendering_context_base-cc.patch"


def added_lines(patch: str) -> str:
    return "\n".join(
        line[1:]
        for line in patch.splitlines()
        if line.startswith("+") and not line.startswith("+++")
    )


class WebGLCorrectnessRegressionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.webgl1 = WEBGL1.read_text(encoding="utf-8")
        cls.webgl2 = WEBGL2.read_text(encoding="utf-8")
        cls.bridge = BRIDGE.read_text(encoding="utf-8")
        cls.webgl1_added = added_lines(cls.webgl1)
        cls.webgl2_added = added_lines(cls.webgl2)

    def test_identity_is_always_persona_backed(self):
        self.assertIn('kDefaultWebGLVendor[] = "Google Inc. (Intel)"', self.webgl1_added)
        self.assertIn("kDefaultWebGLRenderer[]", self.webgl1_added)
        self.assertIn("WebGLPersonaVendor().c_str()", self.webgl1_added)
        self.assertIn("WebGLPersonaRenderer().c_str()", self.webgl1_added)
        self.assertNotIn("ContextGL()->GetString(GL_RENDERER)", self.webgl1_added)
        self.assertNotIn("ContextGL()->GetString(GL_VENDOR)", self.webgl1_added)
        self.assertNotIn("NVIDIA GeForce RTX 3060", self.webgl1_added)
        self.assertNotIn("Google Inc. (NVIDIA Corporation)", self.webgl1_added)

    def test_configured_identity_requires_a_nonempty_pair(self):
        self.assertEqual(self.webgl1_added.count("!configured_vendor.empty() && !configured_renderer.empty()"), 2)
        self.assertEqual(self.webgl1_added.count('config.Get("uxr-webgl-renderer")'), 2)
        self.assertEqual(self.webgl1_added.count('config.Get("uxr-webgl-vendor")'), 2)
        self.assertIn("ExtensionEnabled(kWebGLDebugRendererInfoName)", self.webgl1)

    def test_webgl1_limits_are_downward_clamped_and_versions_are_standardized(self):
        self.assertIn("ClampWebGLPersonaLimit", self.webgl1_added)
        self.assertIn("ClampWebGLPersonaViewport", self.webgl1_added)
        self.assertIn("real_value > 0 && real_value < persona_value", self.webgl1_added)
        self.assertIn("WebGL GLSL ES 1.0 (OpenGL ES GLSL ES 1.0 Chromium)", self.webgl1_added)
        self.assertIn("WebGL 1.0 (OpenGL ES 2.0 Chromium)", self.webgl1_added)
        self.assertNotIn("ContextGL()->GetString(GL_SHADING_LANGUAGE_VERSION)", self.webgl1_added)
        self.assertNotIn("ContextGL()->GetString(GL_VERSION)", self.webgl1_added)

    def test_webgl2_limits_are_downward_clamped_and_versions_are_standardized(self):
        self.assertIn("ClampWebGL2PersonaLimit", self.webgl2_added)
        self.assertIn("WebGL GLSL ES 3.00 (OpenGL ES GLSL ES 3.0 Chromium)", self.webgl2_added)
        self.assertIn("WebGL 2.0 (OpenGL ES 3.0 Chromium)", self.webgl2_added)
        self.assertNotIn("ContextGL()->GetString(GL_SHADING_LANGUAGE_VERSION)", self.webgl2_added)
        self.assertNotIn("ContextGL()->GetString(GL_VERSION)", self.webgl2_added)
        self.assertIn("4 * WebGL2PersonaVaryingVectors(ContextGL())", self.webgl2_added)

    def test_read_pixels_is_not_modified(self):
        combined = self.webgl1 + self.bridge
        self.assertNotIn("UxrFarbleReadPixels(data", combined)
        self.assertNotIn("GetImageDataCacheFirst(\n+            bridge_canvas_id_, x, y", self.bridge)
        self.assertNotIn("std::memcpy(data, remote->data()", self.bridge)

    def test_buffer_upload_copy_is_bridge_gated(self):
        guard = self.bridge.index("if (auto* bridge = canvas_bridge::CanvasBridgeClient::Get();")
        connected = self.bridge.index("bridge && bridge->Connected()", guard)
        payload = self.bridge.index("std::vector<uint8_t>(bytes, bytes + size)", connected)
        self.assertLess(guard, connected)
        self.assertLess(connected, payload)


if __name__ == "__main__":
    unittest.main()
