import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[2]
PERSONA_H = REPO / "patches" / "0091-components-ungoogled-persona-profile-h.patch"
PERSONA_CC = REPO / "patches" / "0092-components-ungoogled-persona-profile-cc.patch"
PERSONA_BUILD = REPO / "patches" / "0090-components-ungoogled-persona-profile.patch"
WEBGL1 = REPO / "patches" / "0093-webgl1-persona-complete.patch"
WEBGL2 = REPO / "patches" / "0094-webgl2-persona-complete.patch"
PRECISION = REPO / "patches" / "0095-webgl-precision-extensions.patch"
BRIDGE_H = REPO / "patches" / "0096-webgl-bridge-policy-h.patch"
BRIDGE_CC = REPO / "patches" / "0097-webgl-bridge-policy-cc.patch"
LIFECYCLE = REPO / "patches" / "0099-webgl-bridge-lifecycle-cc.patch"
BRIDGE_CLIENT_CC = REPO / "patches" / "0069-third_party-blink-renderer-platform-canvas_bridge-canvas_bridge_client-cc.patch"
BRIDGE_CLIENT_H = REPO / "patches" / "0070-third_party-blink-renderer-platform-canvas_bridge-canvas_bridge_client-h.patch"
READBACK_NOISE = REPO / "patches" / "0100-webgl-readback-noise.patch"
GPU_FP_CC = REPO / "patches" / "0101-webgl-gpu_fingerprint-cc.patch"
GPU_INFO_CC = REPO / "patches" / "0103-webgl-gpu_info-cc.patch"
FARBLE_CC = REPO / "patches" / "0105-components-ungoogled-farble_seed.patch"
GPU_INTEGRATION = REPO / "patches" / "0108-webgl-gpu-fingerprint-integration.patch"


def added_lines(patch: str) -> str:
    return "\n".join(
        line[1:]
        for line in patch.splitlines()
        if line.startswith("+") and not line.startswith("+++")
    )


class WebGLCorrectnessRegressionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.persona_h = added_lines(PERSONA_H.read_text(encoding="utf-8"))
        cls.persona_cc = added_lines(PERSONA_CC.read_text(encoding="utf-8"))
        cls.persona_build = added_lines(PERSONA_BUILD.read_text(encoding="utf-8"))
        cls.webgl1 = added_lines(WEBGL1.read_text(encoding="utf-8"))
        cls.webgl2 = added_lines(WEBGL2.read_text(encoding="utf-8"))
        cls.precision = added_lines(PRECISION.read_text(encoding="utf-8"))
        cls.bridge_h = BRIDGE_H.read_text(encoding="utf-8")
        cls.bridge_cc = BRIDGE_CC.read_text(encoding="utf-8")
        cls.lifecycle = added_lines(LIFECYCLE.read_text(encoding="utf-8"))
        cls.readback_noise = added_lines(READBACK_NOISE.read_text(encoding="utf-8"))
        cls.gpu_fp_cc = added_lines(GPU_FP_CC.read_text(encoding="utf-8"))
        cls.gpu_info_cc = added_lines(GPU_INFO_CC.read_text(encoding="utf-8"))
        cls.farble_cc = added_lines(FARBLE_CC.read_text(encoding="utf-8"))
        cls.integration = added_lines(GPU_INTEGRATION.read_text(encoding="utf-8"))
        cls.client_cc = added_lines(BRIDGE_CLIENT_CC.read_text(encoding="utf-8"))
        cls.client_h = added_lines(BRIDGE_CLIENT_H.read_text(encoding="utf-8"))


    def test_persona_fields_are_declared_and_registered(self):
        self.assertIn("webgl_max_combined_texture_image_units", self.persona_h)
        self.assertIn("webgl_max_combined_texture_image_units", self.persona_cc)
        for source in ("farble_seed.h", "farble_seed.cc", "fingerprint_data.h"):
            self.assertIn(source, self.persona_build)

    def test_gpu_fingerprint_priority_and_seed_path(self):
        self.assertIn("webgl_identity_explicit", self.persona_h)
        self.assertIn("webgl_fingerprint", self.persona_h)
        self.assertIn("ConfigSwitchEnabled", self.persona_cc)
        self.assertIn("GetGLRendererStringForFingerprint", self.integration)
        self.assertIn("GetGLVendorStringForFingerprint", self.integration)
        self.assertIn("effective_seed", self.gpu_fp_cc)

    def test_readback_noise_contract(self):
        self.assertIn("bridge_substituted = true", self.readback_noise)
        self.assertIn("!bridge_substituted", self.readback_noise)
        self.assertIn("format == GL_RGBA", self.readback_noise)
        self.assertIn("type == GL_UNSIGNED_BYTE", self.readback_noise)
        self.assertIn("FingerprintNoiseEnabled()", self.readback_noise)
        self.assertIn("for (int channel = 0; channel < 3; ++channel)", self.readback_noise)

    def test_farble_seed_and_gpu_table_are_present(self):
        self.assertIn("GlobalSeed()", self.farble_cc)
        self.assertIn("PersistentHash(registrable_domain)", self.farble_cc)
        self.assertIn("uxr-disable-fingerprint-noise", self.farble_cc)
        self.assertIn("kGpuModelCount", self.gpu_info_cc)

    def test_identity_is_session_constant_and_persona_backed(self):
        self.assertIn("CurrentPersona().webgl_renderer", self.webgl1)
        self.assertIn("CurrentPersona().webgl_vendor", self.webgl1)
        self.assertNotIn("RTX 3060", self.webgl1)
        self.assertNotIn("Google Inc. (NVIDIA Corporation)", self.webgl1)
        self.assertIn("webgl_real", self.webgl1)

    def test_webgl1_complete_limits_are_downward_clamped(self):
        self.assertIn("ClampPersonaLimit", self.webgl1)
        self.assertIn("ClampPersonaLimitF", self.webgl1)
        self.assertIn("PersonaViewport", self.webgl1)
        self.assertIn("webgl_max_combined_texture_image_units", self.webgl1)
        self.assertIn("webgl_max_fragment_uniform_vectors", self.webgl1)
        self.assertIn("webgl_max_vertex_uniform_vectors", self.webgl1)
        self.assertIn("real > 0 && value > real", self.webgl1)

    def test_webgl2_coherence_and_sample_filtering(self):
        self.assertIn("values", self.webgl2)
        self.assertIn("webgl_max_samples", self.webgl2)
        self.assertIn("value <= max_samples", self.webgl2)
        self.assertGreaterEqual(self.webgl2.count("PersonaMaxVaryingVectors(ContextGL())"), 2)
        self.assertIn("webgl_max_texture_lod_bias", self.webgl2)
        self.assertIn("ClampPersonaLimitF", self.webgl2)

    def test_real_mode_bypasses_spoofed_parameter_tables(self):
        self.assertIn("if (ungoogled::CurrentPersona().webgl_real)", self.webgl1)
        self.assertIn("if (ungoogled::CurrentPersona().webgl_real)", self.webgl2)
        self.assertIn("ContextGL()->GetString(GL_RENDERER)", self.webgl1)
        self.assertIn("ContextGL()->GetString(GL_VENDOR)", self.webgl1)

    def test_precision_and_extension_override_are_persona_controlled(self):
        self.assertIn("CurrentPersona().mobile", self.precision)
        self.assertIn("GL_MEDIUM_FLOAT", self.precision)
        self.assertIn("precision = 10", self.precision)
        self.assertIn("filtered", self.precision)
        self.assertIn("webgl_extensions", self.precision)

    def test_readback_policy_and_direct_read_pixels_bridge(self):
        self.assertIn("BridgeAllowedForThisContext", self.bridge_cc)
        self.assertIn("RegistrableDomain", self.bridge_cc)
        self.assertIn("BridgeEnabledForOrigin", self.bridge_cc)
        self.assertIn("format == GL_RGBA && type == GL_UNSIGNED_BYTE", self.bridge_cc)
        self.assertIn("GetImageDataCacheFirst", self.bridge_cc)
        self.assertIn("webgl_unsupported", self.bridge_cc)

    def test_bridge_policy_is_implemented_and_uses_explicit_modes(self):
        self.assertIn("enum class PolicyMode", self.client_h)
        self.assertIn("BridgeEnabledForOrigin", self.client_h)
        self.assertIn("uxr-canvas-bridge-mode", self.client_cc)
        self.assertIn("uxr-canvas-bridge-allow", self.client_cc)
        self.assertIn("uxr-canvas-bridge-deny", self.client_cc)
        self.assertIn("uxr-canvas-bridge-fallback", self.client_cc)
        self.assertIn("BridgeEnabledForOrigin", self.client_cc)
        self.assertIn("kCreateFramebuffer", self.lifecycle)
        self.assertIn("kCreateRenderbuffer", self.lifecycle)
        self.assertIn("kDeleteObject", self.lifecycle)
        self.assertIn("kBridgeDisabledCanvasId", self.lifecycle)
        self.assertIn("bridge_canvas_id_ = 0", self.lifecycle)
        self.assertIn("bridge_webgl_obj_counter_ = 0", self.lifecycle)


if __name__ == "__main__":
    unittest.main()
