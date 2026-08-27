import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[2]
WEBGL1 = REPO / "patches" / "0029-third_party-blink-renderer-modules-webgl-webgl_rendering_context_base-cc.patch"
WEBGL2 = REPO / "patches" / "0035-webgl2-getparameter-version-normalize.patch"
BRIDGE = REPO / "patches" / "0082-third_party-blink-renderer-modules-webgl-webgl_rendering_context_base-cc.patch"


class WebGLCorrectnessRegressionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.webgl1 = WEBGL1.read_text(encoding="utf-8")
        cls.webgl2 = WEBGL2.read_text(encoding="utf-8")
        cls.bridge = BRIDGE.read_text(encoding="utf-8")

    def test_default_identity_and_capabilities_remain_native(self):
        self.assertNotIn("NVIDIA GeForce RTX 3060", self.webgl1)
        self.assertNotIn("Google Inc. (NVIDIA Corporation)", self.webgl1)
        self.assertNotIn("uxr-webgl-fullparams", self.webgl1)
        self.assertNotIn("GL_MAX_TEXTURE_SIZE", self.webgl1)
        self.assertIn("ContextGL()->GetString(GL_RENDERER)", self.webgl1)
        self.assertIn("ContextGL()->GetString(GL_VENDOR)", self.webgl1)

    def test_identity_override_requires_a_nonempty_pair(self):
        self.assertEqual(self.webgl1.count("!renderer.empty() && !vendor.empty()"), 2)
        self.assertEqual(self.webgl1.count('config.Get("uxr-webgl-renderer")'), 2)
        self.assertEqual(self.webgl1.count('config.Get("uxr-webgl-vendor")'), 2)
        self.assertIn("ExtensionEnabled(kWebGLDebugRendererInfoName)", self.webgl1)

    def test_webgl_version_and_precision_are_backend_derived(self):
        self.assertNotIn("uxr-webgl-renderer", self.webgl2)
        self.assertNotIn("base/uxr_config.h", self.webgl2)
        self.assertNotIn("WebGL 2.0 (OpenGL ES 3.0 Chromium)", self.webgl2)
        self.assertNotIn("WebGLShaderPrecisionFormat", self.webgl1)

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
