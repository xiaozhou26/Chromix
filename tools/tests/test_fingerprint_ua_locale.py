"""UA/locale regressions; optional Chromium source checks use read-only inputs."""
from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[2]


def patch(number):
    return next((ROOT / "patches").glob(f"{number:04d}-*.patch"))


def additions(number):
    return "\n".join(line[1:] for line in patch(number).read_text().splitlines()
                     if line.startswith("+") and not line.startswith("+++"))


def test_renderer_uses_browser_ua_without_rewriting_metadata():
    host = additions(5)
    assert "effective_user_agent.replace" not in host
    assert "effective_user_agent_metadata.full_version" not in host
    assert "std::move(uxr_cfg), base::UxrConfig::kSchemaVersion" in host
    assert "CHECK(snapshot.SetAll(uxr_cfg))" in host


def test_language_getter_has_no_icu_or_v8_side_effects():
    language = additions(17)
    assert "SetICUDefaultLocale" not in language
    assert "LocaleConfigurationChangeNotification" not in language
    assert "uxr-languages" not in language


def test_locale_is_set_before_timezone_monitor_and_isolate_creation():
    startup = additions(19)
    assert "SetICUDefaultLocale" in startup
    assert 'GetSwitchValueASCII("uxr-languages")' in startup
    assert '"uxr-languages"' in additions(62)


def test_product_replacement_preserves_following_tokens(tmp_path):
    text = additions(4)
    start = text.index("std::string ReplaceProductVersion(")
    end = text.index("\n}", start) + 2
    function = text[start:end]
    source = r'''
#include <cassert>
#include <initializer_list>
#include <string>
namespace base {
std::string StrCat(std::initializer_list<std::string> parts) {
  std::string out;
  for (const auto& part : parts) out += part;
  return out;
}
}
''' + function + r'''
int main() {
  assert(ReplaceProductVersion("Chrome/152.0.0.0", "153.0.0.0") ==
         "Chrome/153.0.0.0");
  assert(ReplaceProductVersion("Chrome/152.0.0.0 Mobile Safari/537.36", "153.0.0.0") ==
         "Chrome/153.0.0.0 Mobile Safari/537.36");
  assert(ReplaceProductVersion("Chromium/152.1.2.3 Extra/2", "153.1.2.3") ==
         "Chromium/153.1.2.3 Extra/2");
  assert(ReplaceProductVersion("Other/1", "153.0.0.0") == "Other/1");
}
'''
    compile_and_run(tmp_path, source)


def compile_and_run(tmp_path, source):
    compiler = os.environ.get("CXX") or shutil.which("clang++") or shutil.which("c++")
    if not compiler:
        pytest.skip("a C++ compiler is required for the small stub harness")
    cpp = tmp_path / "harness.cc"
    exe = tmp_path / "harness"
    cpp.write_text(source)
    compiled = subprocess.run([compiler, "-std=c++20", "-Wall", "-Wextra", "-Werror",
                               str(cpp), "-o", str(exe)], capture_output=True, text=True)
    assert compiled.returncode == 0, compiled.stdout + compiled.stderr
    result = subprocess.run([str(exe)], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr


COMMAND_LINE_STUB = r'''
#include <cassert>
#include <map>
#include <optional>
#include <string>
#include <vector>
namespace base {
class CommandLine {
 public:
  std::map<std::string, std::string> values;
  bool HasSwitch(const std::string& key) const { return values.contains(key); }
  std::string GetSwitchValueASCII(const std::string& key) const {
    auto it = values.find(key);
    return it == values.end() ? "" : it->second;
  }
  void AppendSwitchASCII(const std::string& key, const std::string& value) {
    values[key] = value;
  }
  void AppendSwitch(const std::string& key) { values[key] = ""; }
  void RemoveSwitch(const std::string& key) { values.erase(key); }
  const auto& GetSwitches() const { return values; }
  static CommandLine* ForCurrentProcess() { static CommandLine cmd; return &cmd; }
};
}
'''


def block(text, start_text):
    start = text.index(start_text)
    opening = text.index("{", start)
    depth = 1
    end = opening + 1
    while depth:
        depth += (text[end] == "{") - (text[end] == "}")
        end += 1
    return text[start:end]


def test_noise_disable_retains_persona_seed_but_disables_noise(tmp_path):
    text = additions(36)
    noise = block(text, '    if (command_line->HasSwitch("fingerprint-noise")')
    compile_and_run(tmp_path, COMMAND_LINE_STUB + '''
void Normalize(base::CommandLine* command_line) {
''' + noise + r'''
}
int main() {
  base::CommandLine cmd;
  cmd.values = {{"fingerprint-noise", "false"}, {"uxr-fingerprint-seed", "42"},
                {"uxr-canvas-seed", "42"}, {"uxr-audio-seed", "42"}};
  Normalize(&cmd);
  assert(cmd.HasSwitch("uxr-disable-fingerprint-noise"));
  assert(cmd.GetSwitchValueASCII("uxr-fingerprint-seed") == "42");
  assert(cmd.GetSwitchValueASCII("uxr-canvas-seed") == "42");
  assert(cmd.GetSwitchValueASCII("uxr-audio-seed") == "42");
  cmd.values = {{"fingerprint-noise", "true"}, {"uxr-canvas-seed", "42"}};
  Normalize(&cmd);
  assert(!cmd.HasSwitch("uxr-disable-fingerprint-noise"));
  assert(cmd.HasSwitch("uxr-canvas-seed"));
}
''')


def test_seed_normalization_preserves_uint64_and_off(tmp_path):
    text = additions(36)
    seed = block(text, '    if (command_line->HasSwitch("fingerprint")) {\n      std::string seed =')
    off = block(text, '    if (command_line->HasSwitch("fingerprint") &&\n        command_line->GetSwitchValueASCII("fingerprint") == "off")')
    compile_and_run(tmp_path, COMMAND_LINE_STUB + r'''
#include <cstdint>
namespace base {
uint64_t random_value;
uint64_t RandUint64() { return random_value; }
std::string NumberToString(uint64_t value) { return std::to_string(value); }
}
void Normalize(base::CommandLine* command_line) {
''' + seed + '\n' + off + r'''
}
int main() {
  base::CommandLine cmd;
  for (const uint64_t value : {UINT64_C(1), UINT64_C(4294967296), UINT64_MAX}) {
    const auto expected = std::to_string(value);
    cmd.values = {{"fingerprint", ""}};
    base::random_value = value;
    Normalize(&cmd);
    for (const auto* key : {"uxr-fingerprint-seed", "uxr-canvas-seed", "uxr-audio-seed"})
      assert(cmd.GetSwitchValueASCII(key) == expected);
    cmd.values = {{"fingerprint", expected}};
    base::random_value = 1;
    Normalize(&cmd);
    assert(cmd.GetSwitchValueASCII("uxr-fingerprint-seed") == expected);
  }
  cmd.values = {{"fingerprint", ""}};
  base::random_value = 0;
  Normalize(&cmd);
  assert(cmd.GetSwitchValueASCII("uxr-canvas-seed") == "1");
  cmd.values = {{"fingerprint", ""}, {"uxr-fingerprint-seed", "18446744073709551615"}};
  Normalize(&cmd);
  assert(cmd.GetSwitchValueASCII("uxr-canvas-seed") == "18446744073709551615");
  cmd.values = {{"fingerprint", ""}, {"uxr-canvas-seed", "4294967296"}};
  Normalize(&cmd);
  assert(cmd.GetSwitchValueASCII("uxr-fingerprint-seed") == "4294967296");
  cmd.values = {{"fingerprint", "42"}, {"uxr-canvas-seed", "7"}, {"uxr-audio-seed", "9"}};
  Normalize(&cmd);
  assert(cmd.GetSwitchValueASCII("uxr-fingerprint-seed") == "42");
  assert(cmd.GetSwitchValueASCII("uxr-canvas-seed") == "7");
  assert(cmd.GetSwitchValueASCII("uxr-audio-seed") == "9");
  cmd.values["fingerprint"] = "off";
  cmd.values["force-webrtc-ip-handling-policy"] = "disable_non_proxied_udp";
  cmd.values["fingerprint-webrtc-ip"] = "203.0.113.20";
  Normalize(&cmd);
  assert(!cmd.HasSwitch("uxr-fingerprint-seed"));
  assert(!cmd.HasSwitch("uxr-canvas-seed"));
  assert(!cmd.HasSwitch("uxr-audio-seed"));
  assert(!cmd.HasSwitch("uxr-webrtc-policy"));
  assert(cmd.HasSwitch("uxr-webgl-real"));
  assert(cmd.HasSwitch("uxr-disable-fingerprint-noise"));
  assert(cmd.GetSwitchValueASCII("force-webrtc-ip-handling-policy") == "disable_non_proxied_udp");
}
''')


def test_candidate_presentation_does_not_reconfigure_browser_routing():
    text = additions(36)
    assert '{"fingerprint-webrtc-ip",' in text
    for retired in ("fingerprint-webrtc-fake-srflx",
                    "uxr-webrtc-fake-srflx", "uxr-webrtc-policy"):
        assert retired not in text


def test_brand_override_replaces_optional_brand_without_growing_list(tmp_path):
    text = additions(4)
    brand_override = text.index('  if (command_line->HasSwitch("uxr-ua-brand"))')
    start = text.rfind('  const base::CommandLine* command_line =', 0, brand_override)
    end = text.index('\n\n', brand_override)
    selection = text[start:end]
    compile_and_run(tmp_path, COMMAND_LINE_STUB + '''
std::optional<std::string> Select(std::optional<std::string> brand) {
''' + selection + r'''
  return brand;
}
int main() {
  auto* cmd = base::CommandLine::ForCurrentProcess();
  assert(Select(std::nullopt) == "Google Chrome");
  assert(Select("Google Chrome") == "Google Chrome");
  cmd->values["uxr-fingerprint-off"] = "true";
  assert(!Select(std::nullopt));
  assert(Select("Existing Brand") == "Existing Brand");
  cmd->values.clear();
  cmd->values["uxr-ua-brand"] = "Example Browser";
  assert(Select("Google Chrome") == "Example Browser");
  cmd->values["uxr-ua-brand"] = "Chromium";
  assert(!Select("Google Chrome"));
  cmd->values["uxr-ua-brand"] = "";
  assert(!Select(std::nullopt));
}
''')
    assert "brand_version_list.emplace_back" not in text


def test_explicit_off_restores_native_headless_product(tmp_path):
    # The insertion and closing brace are unchanged context in the regenerated
    # patch, so take the new side of each hunk rather than additions alone.
    text = '\n'.join(line[1:] for line in patch(4).read_text().splitlines()
                     if line.startswith((' ', '+')) and not line.startswith('+++'))
    branch = block(text, '  if (command_line->GetSwitchValueASCII("uxr-fingerprint-off") == "true" &&')
    compile_and_run(tmp_path, COMMAND_LINE_STUB + r'''
const char* kHeadless = "headless";
std::string Product() {
  const auto* command_line = base::CommandLine::ForCurrentProcess();
  std::string product = "Chrome/152.0.0.0";
''' + branch + r'''
  return product;
}
int main() {
  auto* cmd = base::CommandLine::ForCurrentProcess();
  cmd->values = {{"headless", ""}};
  assert(Product() == "Chrome/152.0.0.0");
  cmd->values["uxr-fingerprint-off"] = "true";
  assert(Product() == "HeadlessChrome/152.0.0.0");
  cmd->values.erase("headless");
  assert(Product() == "Chrome/152.0.0.0");
}
''')


def high_entropy_assignments():
    text = additions(4)
    start = text.index('  metadata.full_version =')
    end = text.index('          : GetPlatformVersion();', start) + len('          : GetPlatformVersion();')
    return text[start:end]


def test_explicit_high_entropy_values_do_not_read_host(tmp_path):
    assignments = high_entropy_assignments()
    compile_and_run(tmp_path, COMMAND_LINE_STUB + r'''
int host_reads = 0;
std::string GetEffectiveBrowserBrandFullVersion() { return "153.1.2.3"; }
std::string GetCpuArchitecture() { ++host_reads; return "host-arch"; }
std::string BuildModelInfo() { ++host_reads; return "host-model"; }
std::string GetCpuBitness() { ++host_reads; return "host-bits"; }
bool IsWoW64() { ++host_reads; return true; }
std::string GetPlatformVersion() { ++host_reads; return "host-version"; }
struct Metadata {
  std::string full_version, architecture, model, bitness, platform_version;
  bool wow64 = false;
};
Metadata Read() {
  Metadata metadata;
''' + assignments + r'''
  return metadata;
}
int main() {
  auto* cmd = base::CommandLine::ForCurrentProcess();
  cmd->values = {{"uxr-ua-arch", "x86"}, {"uxr-ua-model", ""},
      {"uxr-ua-bitness", "64"}, {"uxr-ua-wow64", "false"},
      {"uxr-ua-platform-version", "15.0.0"}};
  auto m = Read();
  assert(host_reads == 0);
  assert(m.architecture == "x86" && m.bitness == "64" && m.model.empty());
  assert(!m.wow64 && m.platform_version == "15.0.0");
  cmd->values.clear();
  m = Read();
  assert(host_reads == 5 && m.architecture == "host-arch" && m.wow64);
}
''')


def test_locale_normalization_is_after_off_and_before_preferences():
    text = additions(36)
    assert text.index('GetSwitchValueASCII("fingerprint") == "off"') < text.index('std::vector<std::string> languages;')
    assert 'LanguageTagConverter::GetInstance().FromString(value)' in text
    assert 'GetLanguageTagFromString(value)' not in text
    assert 'tag->language_subtag() == "und"' in text
    assert 'base::JoinString(languages, ",")' in text
    assert 'AppendSwitchASCII(switches::kAcceptLang, normalized)' in text
    assert 'RemoveSwitch("uxr-languages")' in text
    assert '"X11; Linux x86_64"' in text


def test_fingerprint_aliases_preserve_explicit_native_switches(tmp_path):
    text = additions(36)
    start = text.index('    struct Alias ')
    aliases = text[start:text.index('    for (const auto& a : kAliases)', start)]
    normalization = text[start:text.index('    // Canonicalize the public off spellings')]
    compile_and_run(tmp_path, COMMAND_LINE_STUB + '''
void Normalize(base::CommandLine* command_line) {
''' + normalization + '''
}
int main() {
  base::CommandLine cmd;
''' + aliases + r'''
  for (const auto& alias : kAliases) {
    cmd.values = {{alias.fp, "alias-value"}};
    Normalize(&cmd);
    assert(cmd.GetSwitchValueASCII(alias.uxr) == "alias-value");
    for (const auto& value : {"native-value", ""}) {
      cmd.values = {{alias.fp, "alias-value"}, {alias.uxr, value}};
      Normalize(&cmd);
      assert(cmd.HasSwitch(alias.uxr));
      assert(cmd.GetSwitchValueASCII(alias.uxr) == value);
    }
  }
  cmd.values = {{"fingerprint-locale", "en-US"}};
  Normalize(&cmd);
  assert(cmd.GetSwitchValueASCII("uxr-languages") == "en-US");
  for (const auto& value : {"zh-CN", ""}) {
    cmd.values = {{"fingerprint-locale", "en-US"}, {"uxr-languages", value}};
    Normalize(&cmd);
    assert(cmd.HasSwitch("uxr-languages"));
    assert(cmd.GetSwitchValueASCII("uxr-languages") == value);
  }
  cmd.values.clear();
  Normalize(&cmd);
  assert(cmd.values.empty());
}
''')


@pytest.mark.parametrize("host_os", ["windows", "macos", "linux", "other"])
@pytest.mark.parametrize("host_arch,host_bits", [("arm", "64"), ("x86", "32")])
def test_platform_aliases_preserve_native_hints_and_fill_cross_os_templates(
        tmp_path, host_os, host_arch, host_bits):
    text = additions(36)
    start = text.index('    struct Alias ')
    normalization = text[start:text.index('    if (command_line->HasSwitch("fingerprint")) {\n      std::string seed =')]
    off = block(text, '    if (command_line->HasSwitch("fingerprint") &&\n        command_line->GetSwitchValueASCII("fingerprint") == "off")')
    flags = '#define BUILDFLAG(flag) (BUILDFLAG_INTERNAL_##flag())\n'
    for flag, os_name in (("IS_WIN", "windows"), ("IS_MAC", "macos"), ("IS_LINUX", "linux")):
        flags += f'#define BUILDFLAG_INTERNAL_{flag}() {int(host_os == os_name)}\n'
    ua_platform = {"windows": "Windows", "macos": "macOS", "linux": "Linux", "other": "Other"}[host_os]
    host = (f'const std::string kHostPlatform = "{ua_platform}";\n'
            f'const std::string kHostArch = "{host_arch}";\n'
            f'const std::string kHostBits = "{host_bits}";\n')
    compile_and_run(tmp_path, flags + COMMAND_LINE_STUB + host + r'''
#include <algorithm>
#include <cctype>
namespace base {
std::string ToLowerASCII(std::string value) {
  std::transform(value.begin(), value.end(), value.begin(),
                 [](unsigned char ch) { return std::tolower(ch); });
  return value;
}
}
void Normalize(base::CommandLine* command_line) {
''' + normalization + off + r'''
}
int host_reads = 0;
std::string GetEffectiveBrowserBrandFullVersion() { return "153.1.2.3"; }
std::string GetCpuArchitecture() { ++host_reads; return kHostArch; }
std::string BuildModelInfo() { ++host_reads; return "host-model"; }
std::string GetCpuBitness() { ++host_reads; return kHostBits; }
bool IsWoW64() { ++host_reads; return true; }
std::string GetPlatformVersion() { ++host_reads; return "host-version"; }
struct Metadata {
  std::string full_version, architecture, model, bitness, platform_version;
  bool wow64 = false;
};
Metadata ReadHints() {
  Metadata metadata;
''' + high_entropy_assignments() + r'''
  return metadata;
}
void AssertOff(const base::CommandLine& cmd) {
  assert(cmd.HasSwitch("uxr-webgl-real"));
  assert(cmd.HasSwitch("uxr-disable-fingerprint-noise"));
  for (const auto& [key, value] : cmd.values)
    assert(key.compare(0, 4, "uxr-") != 0 || key == "uxr-webgl-real" ||
           key == "uxr-disable-fingerprint-noise" || key == "uxr-fingerprint-off");
}
int main() {
  const struct {
    const char* alias;
    const char* platform;
    const char* ua_platform;
    const char* os;
    const char* version;
  } cases[] = {
      {"windows", "Win32", "Windows", "Windows NT 10.0; Win64; x64", "10.0.0"},
      {"WINDOWS", "Win32", "Windows", "Windows NT 10.0; Win64; x64", "10.0.0"},
      {"Win32", "Win32", "Windows", "Windows NT 10.0; Win64; x64", "10.0.0"},
      {"macos", "MacIntel", "macOS", "Macintosh; Intel Mac OS X 10_15_7", "10.15.7"},
      {"MACOS", "MacIntel", "macOS", "Macintosh; Intel Mac OS X 10_15_7", "10.15.7"},
      {"MacIntel", "MacIntel", "macOS", "Macintosh; Intel Mac OS X 10_15_7", "10.15.7"},
      {"linux", "Linux x86_64", "Linux", "X11; Linux x86_64", ""},
      {"LINUX", "Linux x86_64", "Linux", "X11; Linux x86_64", ""},
      {"Linux x86_64", "Linux x86_64", "Linux", "X11; Linux x86_64", ""},
      {"LINUX X86_64", "Linux x86_64", "Linux", "X11; Linux x86_64", ""},
  };
  const char* high_entropy[] = {"uxr-ua-arch", "uxr-ua-bitness", "uxr-ua-model",
                               "uxr-ua-wow64", "uxr-ua-platform-version"};
  const char* overrides[] = {"uxr-platform", "uxr-ua-platform", "uxr-ua-os",
                            "uxr-ua-arch", "uxr-ua-bitness", "uxr-ua-model",
                            "uxr-ua-wow64", "uxr-ua-platform-version"};
  base::CommandLine& cmd = *base::CommandLine::ForCurrentProcess();
  for (const auto& test : cases) {
    const std::string alias = base::ToLowerASCII(test.alias);
    const bool native = kHostPlatform == test.ua_platform;
    const bool explicit_x86 = alias == "linux x86_64";
    cmd.values = {{"fingerprint-platform", test.alias}};
    Normalize(&cmd);
    if (native && !explicit_x86) {
      assert(!cmd.HasSwitch("uxr-ua-platform"));
      assert(!cmd.HasSwitch("uxr-platform"));
      assert(!cmd.HasSwitch("uxr-ua-os"));
    } else {
      assert(cmd.GetSwitchValueASCII("uxr-ua-platform") == test.ua_platform);
      assert(cmd.GetSwitchValueASCII("uxr-platform") == test.platform);
      assert(cmd.GetSwitchValueASCII("uxr-ua-os") == test.os);
    }
    for (const auto* key : high_entropy) {
      const bool isa_hint = std::string(key) == "uxr-ua-arch" ||
                            std::string(key) == "uxr-ua-bitness";
      assert(cmd.HasSwitch(key) == (!native || (explicit_x86 && isa_hint)));
    }
    host_reads = 0;
    const auto hints = ReadHints();
    assert(host_reads == (native ? (explicit_x86 ? 3 : 5) : 0));
    assert(hints.architecture == (!native || explicit_x86 ? "x86" : kHostArch));
    assert(hints.bitness == (!native || explicit_x86 ? "64" : kHostBits));
    assert(hints.platform_version == (native ? "host-version" : test.version));
    assert(hints.model == (native ? "host-model" : ""));
    assert(hints.wow64 == native);
    const auto normalized = cmd.values;
    Normalize(&cmd);
    assert(cmd.values == normalized);
    cmd.values["fingerprint"] = "off";
    Normalize(&cmd);
    AssertOff(cmd);

    cmd.values = {{"fingerprint-platform", test.alias},
                  {"fingerprint-platform-version", "alias-version"}};
    Normalize(&cmd);
    assert(cmd.GetSwitchValueASCII("uxr-ua-platform-version") == "alias-version");
    for (const auto* key : overrides) {
      for (const auto& value : {"explicit-native", ""}) {
        cmd.values = {{"fingerprint-platform", test.alias},
                      {"fingerprint-platform-version", "alias-version"}, {key, value}};
        Normalize(&cmd);
        assert(cmd.HasSwitch(key));
        assert(cmd.GetSwitchValueASCII(key) == value);
      }
    }
    cmd.values = {{"fingerprint-platform", test.alias},
                  {"fingerprint-platform-version", "alias-version"}};
    for (const auto* key : overrides)
      cmd.values[key] = "explicit-native";
    const auto explicit_values = cmd.values;
    Normalize(&cmd);
    assert(cmd.values == explicit_values);
    cmd.values["fingerprint"] = "off";
    Normalize(&cmd);
    AssertOff(cmd);
  }
  for (const auto& alias : {"", " ", " windows", "windows ", "win", "win64",
                           "mac", "darwin", "android", "ios", "chromeos", "freebsd",
                           "Linux aarch64", "Linux x86_64; extra"}) {
    cmd.values = {{"fingerprint-platform", alias}};
    const auto untouched = cmd.values;
    Normalize(&cmd);
    assert(cmd.values == untouched);
    for (const auto* key : overrides)
      cmd.values[key] = "explicit-native";
    const auto explicit_values = cmd.values;
    Normalize(&cmd);
    assert(cmd.values == explicit_values);
    cmd.values["fingerprint"] = "off";
    Normalize(&cmd);
    AssertOff(cmd);
  }
  cmd.values.clear();
  Normalize(&cmd);
  assert(cmd.values.empty());
}
''')


OWNED = (4, 5, 11, 16, 17, 19, 36, 62)


@pytest.mark.parametrize("platform", ["linux", "macos", "windows"])
def test_patches_apply_strictly_to_supplied_real_upstream(tmp_path, platform):
    supplied = os.environ.get("CHROMIX_UA_LOCALE_UPSTREAM_ROOT")
    if not supplied:
        pytest.skip("set CHROMIX_UA_LOCALE_UPSTREAM_ROOT for real-source patch checks")
    source = Path(supplied) / platform / "upstream"
    patch_bin = shutil.which("patch")
    if not patch_bin:
        pytest.skip("GNU patch is required")
    targets = {re.search(r'^\+\+\+ b/(.*)$', patch(n).read_text(), re.M)[1]
               for n in OWNED}
    originals = {}
    for target in targets:
        src = source / target
        originals[target] = (src.read_bytes(), src.stat().st_mtime_ns)
        dest = tmp_path / target
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(originals[target][0])
    selected = []
    for name in (ROOT / "patches/series").read_text().splitlines():
        if not name or name.startswith("#"):
            continue
        path = ROOT / name
        target = re.search(r'^\+\+\+ b/(.*)$', path.read_text(), re.M)[1]
        if target in targets:
            selected.append(path)
    def apply(path, reverse=False):
        command = [patch_bin, "-p1", "--fuzz=0", "--batch", "--no-backup-if-mismatch",
                   "--reject-file=-", "--input", str(path)]
        if reverse:
            command.append("--reverse")
        result = subprocess.run(command, cwd=tmp_path, capture_output=True, text=True)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "fuzz" not in result.stdout
    for path in selected:
        apply(path)
    ua = (tmp_path / "components/embedder_support/user_agent_utils.cc").read_text()
    metadata = block(ua, "blink::UserAgentMetadata GetUserAgentMetadata(")
    assert metadata.index("if (only_low_entropy_ch)") < metadata.index('HasSwitch("uxr-ua-arch")')
    assert metadata.index("if (custom_ua.has_value())") < metadata.index('HasSwitch("uxr-ua-arch")')
    host = (tmp_path / "content/browser/renderer_host/render_process_host_impl.cc").read_text()
    assert "effective_user_agent.replace" not in host
    assert "effective_user_agent_metadata.full_version" not in host
    assert host.index("GetRendererInterface()->SetUxrConfig(") < host.index("GetRendererInterface()->InitializeRenderer(")
    language = (tmp_path / "third_party/blink/renderer/core/frame/navigator_language.cc").read_text()
    assert "SetICUDefaultLocale" not in language
    assert "probe::ApplyAcceptLanguageOverride" in language
    assert "network::features::kReduceAcceptLanguage" in language
    startup = (tmp_path / "third_party/blink/renderer/core/timezone/timezone_controller.cc").read_text()
    init = block(startup, "void TimeZoneController::Init()")
    assert init.index("SetICUDefaultLocale") < init.index("kTopChromeWebUI")
    for path in reversed(selected):
        apply(path, reverse=True)
    for target, state in originals.items():
        assert (tmp_path / target).read_bytes() == state[0]
        src = source / target
        assert (src.read_bytes(), src.stat().st_mtime_ns) == state


def test_local_source_confirms_startup_and_network_call_paths():
    source = ROOT / ".chromix-build-verify/src"
    if not source.is_dir():
        pytest.skip("optional read-only Chromium source tree is unavailable")
    read = lambda path: (source / path).read_text()
    controller = read("third_party/blink/renderer/controller/blink_initializer.cc")
    assert controller.index("GetBlinkInitializer().Initialize();") < controller.index("V8Initializer::InitializeMainThread();")
    core = read("third_party/blink/renderer/core/core_initializer.cc")
    assert "TimeZoneController::Init();" in core
    renderer = read("content/renderer/render_thread_impl.cc")
    assert "user_agent_metadata_ = user_agent_metadata;" in renderer
    client = read("chrome/browser/chrome_content_browser_client.cc")
    assert "return embedder_support::GetUserAgentMetadata();" in client
    network = read("content/browser/client_hints/client_hints.cc")
    assert "ua_metadata = delegate->GetUserAgentMetadata();" in network
    assert "ua_metadata->SerializeBrandMajorVersionList()" in network
    prefs = read("chrome/browser/prefs/chrome_command_line_pref_store.cc")
    assert "{switches::kAcceptLang, language::prefs::kSelectedLanguages}" in prefs
    isolate = read("v8/src/execution/isolate.cc")
    assert "const std::string& Isolate::DefaultLocale()" in isolate
    assert "icu::Locale default_locale;" in isolate
