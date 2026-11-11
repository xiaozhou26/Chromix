"""macOS 156 product regressions; extracted C++ tests are not runtime acceptance."""
from pathlib import Path
import hashlib
import json
import os
import shutil
import subprocess

import pytest

from test_media_stage9 import media, stage9_report

ROOT = Path(__file__).resolve().parents[2]
PATCHES = ROOT / 'patches/chromium156-additions'
ANGLE = PATCHES / '0234-macos-empty-angle-native-default.patch'
HEVC = PATCHES / '0235-macos-hevc-recorder-mime.patch'


def postimage(patch):
    return '\n'.join(line[1:] for line in patch.read_text().splitlines()
                     if line.startswith((' ', '+')) and not line.startswith('+++'))


def compile_run(tmp_path, source):
    compiler = shutil.which('clang++') or shutil.which('g++')
    if not compiler:
        pytest.skip('C++ compiler required')
    cpp = tmp_path / 'regression.cc'
    cpp.write_text(source)
    binary = tmp_path / 'regression'
    result = subprocess.run([compiler, '-std=c++20', '-Wall', '-Wextra', '-Werror',
                             str(cpp), '-o', str(binary)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    subprocess.run([str(binary)], check=True, timeout=10)


@pytest.mark.parametrize('mac', [0, 1])
def test_empty_angle_uses_default_only_on_macos_without_overriding_explicit_backends(tmp_path, mac):
    source = postimage(ANGLE)
    selection = source[source.index('  bool use_angle_default ='):source.index('\n\n')]
    compile_run(tmp_path, r'''
#include <cassert>
#include <string>
#define BUILDFLAG(flag) flag
#define IS_MAC ''' + str(mac) + r'''
namespace switches { constexpr char kUseANGLE[] = "use-angle"; }
struct CommandLine { bool present; bool HasSwitch(const char*) const {return present;} };
int main() {
  for (bool present : {false, true}) for (bool software : {false, true}) {
    for (std::string renderer : {"", "default", "metal", "gl", "vulkan", "swiftshader", "null", "invalid"}) {
      CommandLine command{present}; auto* command_line = &command;
      bool force_software_gl = software;
      std::string requested_renderer = renderer;
      const char* kANGLEImplementationDefaultName = "default";
''' + selection + r'''
      assert(use_angle_default == (!software && (!present || renderer == "default" ||
                                                (IS_MAC && renderer.empty()))));
    }
  }
}
''')


def test_hevc_suffix_uses_actual_profile_compatibility_tier_level_and_constraints(tmp_path):
    source = postimage(HEVC)
    start = source.index('std::string HEVCCodecSuffix(')
    function = source[start:source.index('\n}\n', start) + 3]
    compile_run(tmp_path, r'''
#include <cassert>
#include <cstdint>
#include <cstdio>
#include <string>
namespace base {
template <typename... Args> std::string StringPrintf(const char* format, Args... args) {
  char result[100]; std::snprintf(result, sizeof(result), format, args...); return result;
}
}
namespace media::mp4 {
struct HEVCDecoderConfigurationRecord {
  uint8_t general_profile_space = 0, general_profile_idc = 1, general_tier_flag = 0;
  uint8_t general_level_idc = 63;
  uint32_t general_profile_compatibility_flags = 0x60000000;
  uint64_t general_constraint_indicator_flags = 0xb00000000000;
};
}
''' + function + r'''
int main() {
  media::mp4::HEVCDecoderConfigurationRecord config;
  assert(HEVCCodecSuffix(config) == ".1.6.L63.B0");
  config.general_level_idc = 93;
  assert(HEVCCodecSuffix(config) == ".1.6.L93.B0");
  config.general_profile_space = 2; config.general_profile_idc = 2;
  config.general_tier_flag = 1; config.general_profile_compatibility_flags = 0x20000000;
  config.general_constraint_indicator_flags = 0x000100000002;
  assert(HEVCCodecSuffix(config) == ".B2.4.H93.00.01.00.00.00.02");
  config.general_constraint_indicator_flags = 0;
  assert(HEVCCodecSuffix(config) == ".B2.4.H93");
  for (int bit = 0; bit < 32; ++bit) {
    config.general_profile_compatibility_flags = uint32_t{1} << bit;
    const auto expected = base::StringPrintf(".B2.%X.H93", uint32_t{1} << (31 - bit));
    assert(HEVCCodecSuffix(config) == expected);
  }
}
''')


def test_hevc_mime_uses_encoded_description_and_keeps_native_support_checks():
    source = postimage(HEVC)
    assert 'config.Parse(last_seen_codec_description_)' in source
    assert 'last_seen_codec_description_ = *codec_description;' in source
    assert 'add_parameter_sets_in_bitstream_ ? "hev1" : "hvc1"' in source
    assert '#if BUILDFLAG(IS_MAC) && BUILDFLAG(ENABLE_HEVC_PARSER_AND_HW_DECODER)' in source
    assert 'CanSupportMimeTypeForCodec' not in HEVC.read_text()
    assert 'IsEncoderSupportedVideoType' not in HEVC.read_text()


@pytest.mark.parametrize('mutation,error', [
    ({'status': 'rejected', 'error': 'NotSupportedError:constructor'}, 'failed to record'),
    ({'mimeSupported': False}, 'serialized MIME'),
    ({'bytes': 0, 'data': ''}, 'malformed media policy evidence'),
])
def test_hevc_native_gate_still_rejects_constructor_mime_and_empty_recording(mutation, error):
    report = stage9_report()
    report['runs'][0]['observation']['families']['hevc']['recorded'].update(mutation)
    assert any(error in item for item in media.assess(report)[0])


@pytest.mark.parametrize('patch,target,sha256', [
    (ANGLE, 'ui/gl/init/gl_display_initializer.cc',
     '75e3955bf3090715e67b363178dfd46c28b93c86e97687c68e6bbc940483f2fe'),
    (HEVC, 'third_party/blink/renderer/modules/mediarecorder/media_recorder_handler.cc',
     '35f467590d8fc68482e71522690d7bd806a5c067293f86025b834c3ed7e12eb5'),
])
def test_exact_stage3_patch_roundtrip(tmp_path, patch, target, sha256):
    root = os.environ.get('CHROMIX_MAC156_HEVC_SOURCE_ROOT')
    if not root:
        pytest.skip('unmodified stage3 source root required')
    original = (Path(root) / target).read_bytes()
    assert hashlib.sha256(original).hexdigest() == sha256
    destination = tmp_path / target
    destination.parent.mkdir(parents=True)
    destination.write_bytes(original)
    for reverse in (False, True):
        command = ['patch', '--batch', '--fuzz=0', '-p1', '-i', str(patch)]
        if reverse:
            command.append('-R')
        result = subprocess.run(command, cwd=tmp_path, text=True, capture_output=True, timeout=10)
        assert result.returncode == 0, result.stdout + result.stderr
        assert 'offset' not in result.stdout and 'fuzz' not in result.stdout
    assert destination.read_bytes() == original


@pytest.fixture(scope='module')
def runtime_report(tmp_path_factory):
    browser = os.environ.get('CHROMIX_MAC156_HEVC_BROWSER')
    if not browser:
        pytest.skip('rebuilt macOS ARM runtime required')
    report = media.run(Path(browser))
    output = tmp_path_factory.mktemp('hevc-runtime') / 'media-policy.json'
    output.write_text(json.dumps(report, indent=2))
    return report


def test_repaired_runtime_really_encodes_decodes_and_rejects_disabled_hevc(runtime_report):
    assert len(runtime_report['runs']) == 3
    for run in runtime_report['runs']:
        assert run['launch_args'] == media.case_args(run['name'])
        hevc = run['observation']['families']['hevc']
        if run['name'] != 'native':
            assert not any(hevc[key] for key in media.API_KEYS)
            for key in ('encoded', 'decoded', 'recorded', 'playback', 'mse'):
                assert media.rejected(hevc[key]), hevc[key]
            continue
        assert all(hevc[key] for key in media.API_KEYS)
        assert media.encoded_errors(hevc['encoded']) == []
        assert media.recorded_errors(hevc['recorded']) == []
        assert hevc['recorded']['mimeSupported'] is True
        assert hevc['recorded']['requestedMime'] == hevc['mime']
        assert hevc['recorded']['mime'].startswith('video/mp4;codecs=hvc1.')
        assert media.decoded_errors(hevc['decoded'], frames=True) == []
        for key in ('playback', 'mse'):
            assert media.decoded_errors(hevc[key]) == []


def test_repaired_runtime_full_media_gate(runtime_report):
    assert runtime_report['errors'] == [], runtime_report['errors']
    assert runtime_report['gaps'] == [], runtime_report['gaps']
    assert runtime_report['status'] == 'passed'
