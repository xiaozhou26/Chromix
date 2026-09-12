"""Executable contracts for the public feature switches (not native-build acceptance)."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[2]


def added(number):
    path = next((ROOT / 'patches').glob(f'{number:04d}-*.patch'))
    return '\n'.join(line[1:] for line in path.read_text(encoding='utf-8').splitlines()
                     if line.startswith('+') and not line.startswith('+++')) + '\n'


def block(text, start):
    index = text.index(start)
    opening = text.index('{', index)
    depth, end = 1, opening + 1
    while depth:
        depth += (text[end] == '{') - (text[end] == '}')
        end += 1
    return text[index:end]


CPP_BASE = r'''
#include <algorithm>
#include <array>
#include <cassert>
#include <charconv>
#include <cmath>
#include <cstdint>
#include <cstring>
#include <iostream>
#include <limits>
#include <map>
#include <optional>
#include <string>
#include <string_view>
#include <vector>
namespace base {
class CommandLine {
 public:
  std::map<std::string, std::string> values;
  bool HasSwitch(const std::string& key) const { return values.contains(key); }
  std::string GetSwitchValueASCII(const std::string& key) const {
    auto it = values.find(key); return it == values.end() ? "" : it->second;
  }
  void AppendSwitchASCII(const std::string& key, const std::string& value) { values[key] = value; }
  void AppendSwitch(const std::string& key) { values[key] = ""; }
  void RemoveSwitch(const std::string& key) { values.erase(key); }
  const auto& GetSwitches() const { return values; }
  static CommandLine* ForCurrentProcess() { static CommandLine command; return &command; }
};
std::string ToLowerASCII(std::string value) {
  for (char& c : value) if (c >= 'A' && c <= 'Z') c += 'a' - 'A';
  return value;
}
std::string ToUpperASCII(std::string value) {
  for (char& c : value) if (c >= 'a' && c <= 'z') c -= 'a' - 'A';
  return value;
}
bool EqualsCaseInsensitiveASCII(const std::string& a, const std::string& b) {
  return ToLowerASCII(a) == ToLowerASCII(b);
}
uint64_t RandUint64() { return 42; }
std::string NumberToString(uint64_t value) { return std::to_string(value); }
bool StringToUint64(const std::string& value, uint64_t* out) {
  auto result = std::from_chars(value.data(), value.data() + value.size(), *out);
  return result.ec == std::errc() && result.ptr == value.data() + value.size();
}
bool StringToInt(const std::string& value, int* out) {
  auto result = std::from_chars(value.data(), value.data() + value.size(), *out);
  return result.ec == std::errc() && result.ptr == value.data() + value.size();
}
bool StringToDouble(const std::string& value, double* out) {
  auto result = std::from_chars(value.data(), value.data() + value.size(), *out);
  return result.ec == std::errc() && result.ptr == value.data() + value.size();
}
struct UxrConfig {
  std::map<std::string, std::string> values;
  static UxrConfig& GetInstance() { static UxrConfig config; return config; }
  bool Has(const std::string& key) const { return values.contains(key); }
  std::string Get(const std::string& key) const {
    auto it = values.find(key); return it == values.end() ? "" : it->second;
  }
  bool GetUint64(const char* key, uint64_t* out) const { return StringToUint64(Get(key), out); }
  bool GetInt(const char* key, int* out) const { return StringToInt(Get(key), out); }
  bool GetDouble(const char* key, double* out) const { return StringToDouble(Get(key), out); }
};
[[maybe_unused]] constexpr int TRIM_WHITESPACE = 1, SPLIT_WANT_NONEMPTY = 1;
std::vector<std::string> SplitString(const std::string& text, const std::string& separators, int, int) {
  std::vector<std::string> result;
  for (size_t begin = 0; begin < text.size();) {
    size_t end = text.find_first_of(separators, begin);
    if (end == std::string::npos) end = text.size();
    auto value = text.substr(begin, end - begin);
    const auto first = value.find_first_not_of(" \t");
    const auto last = value.find_last_not_of(" \t");
    if (first != std::string::npos) result.push_back(value.substr(first, last - first + 1));
    begin = end + 1;
  }
  return result;
}
std::string JoinString(const std::vector<std::string>& values, const std::string& separator) {
  std::string result;
  for (const auto& value : values) { if (!result.empty()) result += separator; result += value; }
  return result;
}
namespace i18n {
struct Tag {
  std::string value;
  std::string language_subtag() const { return value.substr(0, value.find('-')); }
  std::string tag_string() const { return value; }
};
struct LanguageTagConverter {
  static LanguageTagConverter& GetInstance() { static LanguageTagConverter converter; return converter; }
  std::optional<Tag> FromString(const std::string& value) const {
    if (value.empty() || value.find_first_not_of("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-") != std::string::npos)
      return std::nullopt;
    return Tag{value};
  }
};
}
}
namespace switches {
[[maybe_unused]] constexpr const char* kProcessType = "type";
[[maybe_unused]] constexpr const char* kAcceptLang = "accept-lang";
}
'''


def compile_cpp(directory, source, name='contract', libraries=()):
    compiler = os.environ.get('CXX') or shutil.which('clang++') or shutil.which('c++')
    if not compiler:
        pytest.skip('C++20 compiler required')
    directory.mkdir(parents=True, exist_ok=True)
    cpp = directory / (name + '.cc')
    binary = directory / (name + ('.exe' if os.name == 'nt' else ''))
    cpp.write_text(source, encoding='utf-8')
    built = subprocess.run([compiler, '-std=c++20', '-Wall', '-Wextra', '-Werror',
                            str(cpp), '-o', str(binary), *libraries], capture_output=True, text=True)
    assert built.returncode == 0, built.stdout + built.stderr
    return binary


@pytest.fixture(scope='module', params=['windows', 'macos', 'linux'])
def normalizer(request, tmp_path_factory):
    flags = '#define BUILDFLAG(x) x\n'
    for name, os_name in [('IS_WIN', 'windows'), ('IS_MAC', 'macos'), ('IS_LINUX', 'linux')]:
        flags += f'#define {name} {int(request.param == os_name)}\n'
    body = block(added(36), '  if (!command_line->HasSwitch(switches::kProcessType))')
    source = flags + CPP_BASE + '\nvoid Normalize(base::CommandLine* command_line) {\n' + body + r'''
}
int main(int argc, char** argv) {
  auto* command_line = base::CommandLine::ForCurrentProcess();
  for (int i = 1; i < argc; ++i) {
    std::string arg = argv[i]; size_t separator = arg.find('=');
    command_line->values[arg.substr(2, separator == std::string::npos ? separator : separator - 2)] =
        separator == std::string::npos ? "" : arg.substr(separator + 1);
  }
  Normalize(command_line);
  for (const auto& [key, value] : command_line->values) std::cout << key << '=' << value << '\n';
}
'''
    binary = compile_cpp(tmp_path_factory.mktemp('feature-normalizer-' + request.param), source)
    return request.param, binary


def normalize(fixture, *args):
    result = subprocess.run([str(fixture[1]), *args], capture_output=True, text=True, check=True)
    return dict(line.split('=', 1) for line in result.stdout.splitlines())


def test_public_defaults_do_not_require_synthetic(normalizer):
    values = normalize(normalizer, '--fingerprint=42')
    assert values['uxr-fingerprint-enabled'] == 'true'
    assert 'uxr-synthetic-device-tests' not in values
    assert values['uxr-hw-concurrency'] == values['uxr-device-memory'] == '8'
    assert values['uxr-storage-quota'] == '102400'
    dimensions = ('1440', '900', '95') if normalizer[0] == 'macos' else ('1920', '1080', '48' if normalizer[0] == 'windows' else '0')
    assert tuple(values[key] for key in ('uxr-screen-width', 'uxr-screen-height', 'uxr-taskbar-height')) == dimensions


@pytest.mark.parametrize('platform, dimensions', [('windows', ('1920', '1080', '48')), ('linux', ('1920', '1080', '0')), ('macos', ('1440', '900', '95'))])
def test_cross_platform_defaults(normalizer, platform, dimensions):
    values = normalize(normalizer, '--fingerprint=42', '--fingerprint-platform=' + platform)
    assert tuple(values[key] for key in ('uxr-screen-width', 'uxr-screen-height', 'uxr-taskbar-height')) == dimensions


@pytest.mark.parametrize('off', ['off', 'false', '0', 'disable', 'disabled', 'OFF', 'FALSE'])
def test_off_aliases_strip_identity_but_keep_explicit_region(normalizer, off):
    values = normalize(normalizer, '--fingerprint=' + off, '--fingerprint-platform=windows',
                       '--uxr-fingerprint-seed=9', '--fingerprint-gpu-vendor=Intel', '--fingerprint-hardware-concurrency=4',
                       '--fingerprint-timezone=Asia/Tokyo', '--fingerprint-locale=ja-JP', '--uxr-voices=true')
    assert values['fingerprint'] == 'off'
    assert 'fingerprint-platform' not in values
    assert {key for key in values if key.startswith('uxr-')} == {
        'uxr-fingerprint-off', 'uxr-webgl-real', 'uxr-disable-fingerprint-noise', 'uxr-timezone', 'uxr-languages'}
    assert values['uxr-timezone'] == 'Asia/Tokyo'
    assert values['uxr-languages'] == values['accept-lang'] == 'ja-JP'


def test_noise_only_disables_perturbation(normalizer):
    before = normalize(normalizer, '--fingerprint=18446744073709551615')
    after = normalize(normalizer, '--fingerprint=18446744073709551615', '--fingerprint-noise=false')
    for key, value in before.items():
        assert after[key] == value
    assert 'uxr-disable-fingerprint-noise' in after


@pytest.mark.parametrize('brand, canonical', [('Chrome', 'Google Chrome'), ('Edge', 'Microsoft Edge'), ('Opera', 'Opera'), ('Vivaldi', 'Vivaldi')])
def test_brand_mapping_and_independent_versions(normalizer, brand, canonical):
    values = normalize(normalizer, '--fingerprint=42', '--fingerprint-brand=' + brand, '--fingerprint-brand-version=123.4.5.6')
    assert values['uxr-ua-brand'] == canonical
    assert values['uxr-ua-brand-version'] == '123.4.5.6'
    assert ('uxr-ua-full-version' in values) == (brand == 'Chrome')


def test_cookie_enable_is_not_a_false_presence_switch(normalizer):
    values = normalize(normalizer, '--fingerprint-allow-3p-cookies', '--test-third-party-cookie-phaseout', '--disable-features=AnotherFeature')
    assert 'test-third-party-cookie-phaseout' not in values
    assert values['uxr-allow-3p-cookies'] == 'true'
    assert values['disable-features'] == 'AnotherFeature,ForceThirdPartyCookieBlocking'
    disabled = normalize(normalizer, '--fingerprint-allow-3p-cookies=false')
    assert 'uxr-allow-3p-cookies' not in disabled
    assert 'disable-features' not in disabled


@pytest.mark.parametrize('value', ['FALSE', 'Off', '0', 'disable', 'disabled'])
def test_native_boolean_spellings_match_sdk(normalizer, value):
    result = normalize(normalizer, '--fingerprint=42', '--fingerprint-noise=' + value,
                       '--fingerprint-allow-3p-cookies=' + value,
                       '--fingerprint-sapi-voices=' + value, '--fingerprint-windows-font-metrics=' + value)
    assert 'uxr-disable-fingerprint-noise' in result
    assert 'uxr-allow-3p-cookies' not in result and 'uxr-voices' not in result
    assert result['uxr-windows-font-metrics'] == 'false'


def test_standalone_screen_flag_completes_only_geometry(normalizer):
    result = normalize(normalizer, '--fingerprint-screen-width=2560', '--fingerprint-taskbar-height=40')
    assert result['uxr-screen-width'] == '2560'
    assert result['uxr-screen-height'] == ('900' if normalizer[0] == 'macos' else '1080')
    assert result['uxr-taskbar-height'] == '40'
    assert 'uxr-hw-concurrency' not in result and 'uxr-storage-quota' not in result


def test_windows_voice_default_and_opt_out(normalizer):
    enabled = normalize(normalizer, '--fingerprint=42', '--fingerprint-platform=windows')
    assert enabled['uxr-voices'] == 'true'
    disabled = normalize(normalizer, '--fingerprint=42', '--fingerprint-platform=windows', '--fingerprint-sapi-voices=false')
    assert 'uxr-voices' not in disabled


def test_explicit_values_override_defaults(normalizer):
    values = normalize(normalizer, '--fingerprint=42', '--fingerprint-hardware-concurrency=24',
                       '--fingerprint-device-memory=16', '--fingerprint-screen-width=2560', '--fingerprint-screen-height=1440',
                       '--fingerprint-taskbar-height=40', '--fingerprint-storage-quota=1536', '--fingerprint-windows-font-metrics',
                       '--fingerprint-webrtc-ip=198.51.100.7', '--fingerprint-platform-version=19.0.0')
    expected = {'uxr-hw-concurrency':'24', 'uxr-device-memory':'16', 'uxr-screen-width':'2560', 'uxr-screen-height':'1440',
                'uxr-taskbar-height':'40', 'uxr-storage-quota':'1536', 'uxr-webrtc-ip':'198.51.100.7', 'uxr-ua-platform-version':'19.0.0'}
    assert all(values[key] == value for key, value in expected.items())
    assert 'uxr-windows-font-metrics' in values


@pytest.fixture(scope='module')
def quota_binary(tmp_path_factory):
    helper = block(added(129), 'std::optional<int64_t> FingerprintStorageQuotaBytes()')
    source = CPP_BASE + helper + r'''
int main(int argc, char** argv) {
  if (argc > 1) base::CommandLine::ForCurrentProcess()->values["uxr-storage-quota"] = argv[1];
  auto quota = FingerprintStorageQuotaBytes();
  if (quota) std::cout << *quota; else std::cout << "native";
}
'''
    return compile_cpp(tmp_path_factory.mktemp('quota-policy'), source)


@pytest.mark.parametrize('value, expected', [(None, 'native'), ('0', '0'), ('1', '1048576'), ('102400', str(102400 * 1048576)),
    ('8796093022207', str(8796093022207 * 1048576)), ('8796093022208', 'native'), ('-1', 'native'), ('1.5', 'native'), ('NaN', 'native'), ('1x', 'native')])
def test_quota_units_and_overflow(quota_binary, value, expected):
    args = [str(quota_binary)] + ([] if value is None else [value])
    assert subprocess.check_output(args, text=True) == expected


def test_backend_hooks_not_renderer_quota_fictions():
    source = added(129)
    assert source.count('FingerprintStorageQuotaBytes()') == 3
    assert source.count('if (is_override_enabled)') == 2
    assert '!weak_this->GetQuotaOverrideForStorageKey(storage_key)' in source
    assert 'entry->second.quota_size.has_value()' not in source
    for number in (34, 39):
        assert 'uxr-storage-quota' not in added(number)
    assert 'GetSwitchValueASCII("uxr-allow-3p-cookies") == "true"' in added(130)


def test_shadow_binding_is_opt_in_and_does_not_expose_ua_roots(tmp_path):
    function = block(added(134), 'ShadowRoot* Element::ShadowRootForBindings() const')
    source = CPP_BASE + r'''
enum class ShadowRootMode { kOpen, kClosed, kUserAgent };
struct ShadowRoot { ShadowRootMode mode; ShadowRootMode GetMode() const {return mode;} };
struct RuntimeEnabledFeatures { static bool enabled; static bool FakeShadowRootEnabled(){return enabled;} };
bool RuntimeEnabledFeatures::enabled = false;
struct Element {
  ShadowRoot* root = nullptr;
  ShadowRoot* GetShadowRoot() const { return root; }
  ShadowRoot* OpenShadowRoot() const { return root && root->mode == ShadowRootMode::kOpen ? root : nullptr; }
  ShadowRoot* ShadowRootForBindings() const;
};
''' + function + r'''
int main() {
  Element element; ShadowRoot open{ShadowRootMode::kOpen}, closed{ShadowRootMode::kClosed}, ua{ShadowRootMode::kUserAgent};
  for (bool enabled : {false, true}) {
    RuntimeEnabledFeatures::enabled = enabled;
    element.root = nullptr; assert(element.ShadowRootForBindings() == nullptr);
    element.root = &open; assert(element.ShadowRootForBindings() == &open);
    element.root = &closed; assert(element.ShadowRootForBindings() == (enabled ? &closed : nullptr));
    assert(element.OpenShadowRoot() == nullptr);
    element.root = &ua; assert(element.ShadowRootForBindings() == nullptr);
  }
}
'''
    subprocess.run([str(compile_cpp(tmp_path, source))], check=True)
    assert 'ImplementedAs=ShadowRootForBindings' in added(132)
    assert 'ShadowRoot* ShadowRootForBindings() const;' in added(133)
    assert 'status: "stable"' not in added(131)


def test_off_guards_unconditional_automation_surfaces():
    for number in (13, 32, 46):
        assert 'uxr-fingerprint-off' in added(number)
    assert 'probe::ApplyAutomationOverride' in added(13)
    assert 'uxr-fingerprint-off' in added(62)


def test_voice_table_is_populated_at_native_event_boundary():
    source = added(25)
    assert 'mojom_voices = WindowsSpeechVoiceTable()' in source
    assert 'Re-injected lazily' not in source
    assert 'Microsoft David Desktop' in source and 'Microsoft Haruka Desktop' in source
    assert 'BUILDFLAG(IS_WIN)' in source
