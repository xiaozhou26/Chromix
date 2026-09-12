"""Compile patch helpers verbatim; OS/IPC/Skia shims are not browser acceptance."""
from __future__ import annotations

import os
import re
import subprocess

import pytest

from test_fingerprint_features import CPP_BASE, added, block, compile_cpp


def without_project_includes(source):
    return re.sub(r'^#include "[^\n]+\n', '', source, flags=re.M)


@pytest.fixture(scope='module')
def font_metrics(tmp_path_factory):
    source = '#define BUILDFLAG(x) x\n#define IS_LINUX 1\n' + CPP_BASE + r'''
using SkScalar = float;
constexpr uint32_t SkSetFourByteTag(char a, char b, char c, char d) {
  return (uint32_t(a) << 24) | (uint32_t(b) << 16) | (uint32_t(c) << 8) | uint32_t(d);
}
struct SkString { std::string text; const char* c_str() const { return text.c_str(); } };
struct SkTypeface {
  std::string family = "Arial";
  int upem = 2048;
  std::map<uint32_t, std::vector<uint8_t>> tables;
  void getFamilyName(SkString* out) { out->text = family; }
  size_t getTableData(uint32_t tag, size_t offset, size_t length, void* out) {
    auto& data = tables[tag];
    if (offset >= data.size()) return 0;
    length = std::min(length, data.size() - offset);
    std::memcpy(out, data.data() + offset, length); return length;
  }
  size_t getTableSize(uint32_t tag) { return tables[tag].size(); }
  int getUnitsPerEm() { return upem; }
};
struct SkFont {
  SkTypeface* face; float size = 16;
  SkTypeface* getTypeface() const {return face;}
  float getSize() const { return size; }
};
struct SkFontMetrics {
  float fAscent = -7, fDescent = 2, fLeading = 1, fAvgCharWidth = 0, fXHeight = 0, fCapHeight = 0;
};
''' + block(added(136), 'bool SkFontApplyWindowsMetrics(') + r'''
void word(std::vector<uint8_t>& table, size_t offset, int value) {
  table[offset] = (unsigned(value) >> 8) & 255; table[offset + 1] = unsigned(value) & 255;
}
int main(int argc, char** argv) {
  assert(argc == 12);
  auto& config = base::UxrConfig::GetInstance().values;
  config["uxr-windows-font-metrics"] = "true"; config["uxr-platform"] = "Win32";
  SkTypeface face; face.family = argv[1]; face.upem = std::stoi(argv[2]);
  auto& os2 = face.tables[SkSetFourByteTag('O','S','/','2')]; os2.resize(96);
  auto& hhea = face.tables[SkSetFourByteTag('h','h','e','a')]; hhea.resize(10);
  word(os2, 0, 2); word(os2, 2, 1000); word(os2, 74, std::stoi(argv[3])); word(os2, 76, std::stoi(argv[4]));
  word(hhea, 4, std::stoi(argv[5])); word(hhea, 6, std::stoi(argv[6])); word(hhea, 8, std::stoi(argv[7]));
  word(os2, 62, std::stoi(argv[8])); word(os2, 68, 1500); word(os2, 70, -500); word(os2, 72, 80);
  word(os2, 86, 1000); word(os2, 88, 1400);
  const std::string mode = argv[9];
  if (mode == "missing") os2.resize(20);
  if (mode == "mvar") face.tables[SkSetFourByteTag('M','V','A','R')].resize(4);
  if (mode == "disabled") config["uxr-windows-font-metrics"] = "false";
  if (mode == "absent") config.erase("uxr-windows-font-metrics");
  if (mode == "off") config["uxr-fingerprint-off"] = "true";
  if (mode == "linux") config["uxr-platform"] = "Linux x86_64";
  SkFont font{mode == "null" ? nullptr : &face, std::stof(argv[10])};
  SkFontMetrics metrics;
  bool result = SkFontApplyWindowsMetrics(font, std::string(argv[11]) == "null" ? nullptr : &metrics);
  std::cout << result << ' ' << metrics.fAscent << ' ' << metrics.fDescent << ' ' << metrics.fLeading
            << ' ' << metrics.fAvgCharWidth << ' ' << metrics.fXHeight << ' ' << metrics.fCapHeight;
}
'''
    return compile_cpp(tmp_path_factory.mktemp('windows-metrics-tables'), source)


# Independent design-unit readings from native DirectWrite on this Windows host.
# Font revisions/rasterization are not claimed identical on Linux.
FONT_VECTORS = [
    ('Arial', 2048, 1854, 434, 1854, -434, 67, 67),
    ('Times New Roman', 2048, 1825, 443, 1825, -443, 87, 87),
    ('Consolas', 2048, 1884, 514, 1521, -527, 350, 0),
    ('Segoe UI', 2048, 2210, 514, 2210, -514, 0, 0),
    ('Calibri', 2048, 1950, 550, 1536, -512, 452, 0),
    ('Microsoft YaHei', 2048, 2167, 536, 2167, -536, 0, 0),
    ('SimSun', 256, 220, 36, 220, -36, 36, 36),
]


def run_font(binary, vector, *, selection=64, mode='normal', size=16, output='metrics'):
    return list(map(float, subprocess.check_output([
        str(binary), *map(str, vector[:7]), str(selection), mode, str(size), output], text=True).split()))


@pytest.mark.parametrize('vector', FONT_VECTORS, ids=[v[0] for v in FONT_VECTORS])
@pytest.mark.parametrize('size', [9, 16, 23.5])
def test_font_metrics_match_directwrite_design_units(font_metrics, vector, size):
    values = run_font(font_metrics, vector, size=size)
    scale = size / vector[1]
    assert values == pytest.approx([1, -vector[2] * scale, vector[3] * scale,
                                   vector[7] * scale, 1000 * scale, 1000 * scale, 1400 * scale], rel=1e-5)


@pytest.mark.parametrize('mode', ['missing', 'mvar', 'disabled', 'absent', 'off', 'linux', 'null'])
def test_fonts_without_matching_prerequisites_stay_native(font_metrics, mode):
    assert run_font(font_metrics, FONT_VECTORS[0], mode=mode) == [0, -7, 2, 1, 0, 0, 0]


def test_font_family_fallback_and_bad_font_inputs_are_noop(font_metrics):
    vector = ('Liberation Sans', *FONT_VECTORS[0][1:])
    assert run_font(font_metrics, vector)[0] == 0
    assert run_font(font_metrics, FONT_VECTORS[0], output='null')[0] == 0
    for size in [0, -1, 'nan', 'inf']:
        assert run_font(font_metrics, FONT_VECTORS[0], size=size)[0] == 0
    assert run_font(font_metrics, ('Arial', 0, *FONT_VECTORS[0][2:]))[0] == 0


def test_use_typo_metrics_selection(font_metrics):
    values = run_font(font_metrics, FONT_VECTORS[0], selection=128, size=2048)
    assert values[:4] == [1, -1500, 500, 80]


@pytest.fixture(scope='module')
def webrtc_ip(tmp_path_factory):
    networking = r'''
#ifdef _WIN32
#define NOMINMAX
#include <winsock2.h>
#include <ws2tcpip.h>
#include <io.h>
#include <fcntl.h>
#else
#include <arpa/inet.h>
#endif
#include <sstream>
namespace net {
class IPAddress {
 public:
  int family = 0;
  std::array<unsigned char, 16> data{};
  bool AssignFromIPLiteral(std::string_view value) {
    if (value.find('\0') != std::string_view::npos) return false;
    const std::string text(value);
    if (inet_pton(AF_INET, text.c_str(), data.data()) == 1) {family=AF_INET; return true;}
    if (inet_pton(AF_INET6, text.c_str(), data.data()) == 1) {family=AF_INET6; return true;}
    family=0; return false;
  }
  bool IsZero() const { return std::all_of(data.begin(), data.begin() + (family == AF_INET ? 4 : 16), [](auto b){return b == 0;}); }
  std::string ToString() const {
    char buffer[INET6_ADDRSTRLEN];
    const auto* result = inet_ntop(family, data.data(), buffer, sizeof(buffer));
    return result ? std::string(result) : "";
  }
};
}
'''
    source = CPP_BASE + networking + without_project_includes(added(139)) + r'''
int main(int argc, char** argv) {
  assert(argc >= 3);
#ifdef _WIN32
  _setmode(_fileno(stdin), _O_BINARY); _setmode(_fileno(stdout), _O_BINARY);
#endif
  const std::string mode = argv[1], ip = argv[2];
  std::ostringstream input; input << std::cin.rdbuf();
  if (mode == "candidate") std::cout << blink::RewriteFingerprintCandidate(input.str(), ip);
  else if (mode == "sdp") std::cout << blink::RewriteFingerprintSdp(input.str(), ip);
  else if (mode == "address") std::cout << blink::RewriteFingerprintAddress(input.str(), ip);
  else {
    auto& config = base::UxrConfig::GetInstance().values;
    config["uxr-webrtc-ip"] = ip;
    if (argc > 3) config["uxr-fingerprint-off"] = "true";
    std::cout << blink::FingerprintWebRtcIp();
  }
}
'''
    return compile_cpp(tmp_path_factory.mktemp('webrtc-ip'), source,
                       libraries=('-lws2_32',) if os.name == 'nt' else ())


def rewrite(binary, value, ip='198.51.100.7', mode='candidate'):
    result = subprocess.run([str(binary), mode, ip], input=value.encode(), capture_output=True, check=True)
    return result.stdout.decode()


@pytest.mark.parametrize('source', ['192.168.2.3', '2001:db8::1', 'uuid.local'])
@pytest.mark.parametrize('kind', ['host', 'srflx', 'prflx'])
@pytest.mark.parametrize('target', ['198.51.100.7', '2001:db8::7'])
def test_local_candidates_and_related_addresses(webrtc_ip, source, kind, target):
    candidate = f'candidate:1 1 udp 100 {source} 12345 typ {kind} raddr 10.0.0.1 rport 3000 generation 0'
    expected = candidate.replace(source, target).replace('10.0.0.1', target)
    assert rewrite(webrtc_ip, candidate, target) == expected


def test_turn_relay_allocation_and_zero_placeholders_are_preserved(webrtc_ip):
    candidate = 'candidate:1 1 udp 100 203.0.113.5 12345 typ relay raddr 10.0.0.1 rport 3000'
    assert rewrite(webrtc_ip, candidate) == candidate.replace('10.0.0.1', '198.51.100.7')
    for address in ['0.0.0.0', '::', '0:0:0:0:0:0:0:0']:
        assert rewrite(webrtc_ip, address, mode='address') == address


@pytest.mark.parametrize('value', ['', 'garbage', 'candidate:1', 'candidate:1 1 udp 100 10.0.0.1 9 typ future'])
def test_malformed_candidates_are_not_invented(webrtc_ip, value):
    assert rewrite(webrtc_ip, value) == value


def test_webrtc_sdp_line_endings_and_address_family(webrtc_ip):
    value = 'v=0\r\nc=IN IP4 10.0.0.1\r\na=candidate:1 1 udp 100 10.0.0.1 9 typ host\r\nc=IN IP4 0.0.0.0\r\n'
    expected = value.replace('c=IN IP4 10.0.0.1', 'c=IN IP6 2001:db8::7').replace('10.0.0.1', '2001:db8::7')
    assert rewrite(webrtc_ip, value, '2001:db8::7', 'sdp') == expected
    assert rewrite(webrtc_ip, value, 'not-an-ip', 'sdp') == value


def test_relay_connection_line_remains_the_allocated_address(webrtc_ip):
    sdp = ('v=0\r\nc=IN IP6 2001:db8:0:0:0:0:0:5\r\n'
           'a=candidate:1 1 udp 100 2001:db8::5 3000 typ relay raddr 10.0.0.1 rport 4000\r\n')
    assert rewrite(webrtc_ip, sdp, mode='sdp') == sdp.replace('10.0.0.1', '198.51.100.7')


def test_webrtc_off_and_invalid_auto_do_not_return_an_address(webrtc_ip):
    assert rewrite(webrtc_ip, '', '2001:db8:0:0:0:0:0:7', 'config') == '2001:db8::7'
    assert rewrite(webrtc_ip, '', 'auto', 'config') == ''
    assert subprocess.check_output([str(webrtc_ip), 'config', '198.51.100.7', 'off']) == b''


def test_local_sdp_cache_remains_native_and_remote_sdp_is_not_rewritten():
    for number in (141, 142):
        # The native SDP enters NoteSdpCreated before the presentation copy.
        from pathlib import Path
        path = next((Path(__file__).resolve().parents[2] / 'patches').glob(f'{number:04d}-*'))
        new_side = '\n'.join(line[1:] for line in path.read_text().splitlines()
                             if line.startswith(('+', ' ')) and not line.startswith('+++'))
        assert new_side.index('NoteSdpCreated') < new_side.index('RewriteFingerprintSdp')
    source = added(44)
    assert source.count('sdp = RestoreFingerprintLocalSdp(sdp);') == 2
    assert 'fingerprint_pending_local_sdp_' in source and 'fingerprint_current_local_sdp_' in source
    assert 'RewriteFingerprintSdp(native->Utf8(), ip) == shown' in source
    assert 'pending_remote_description_' not in source
    assert 'current_remote_description_' not in source
    assert '"local-candidate"' in added(140) and '"relay"' in added(140)


@pytest.fixture(scope='module', params=['windows', 'linux', 'macos'])
def gpu_persona(request, tmp_path_factory):
    macros = '#define COMPONENT_EXPORT(x)\n#define BUILDFLAG(x) x\n'
    for name, value in [('IS_WIN', 'windows'), ('IS_MAC', 'macos'), ('IS_LINUX', 'linux')]:
        macros += f'#define {name} {int(request.param == value)}\n'
    source = macros + CPP_BASE + without_project_includes(added(91)) + without_project_includes(added(92)) + r'''
int main(int argc, char** argv) {
  auto& config = base::UxrConfig::GetInstance().values;
  for (int i=1; i<argc; ++i) {
    std::string arg = argv[i]; auto equals = arg.find('=');
    config[arg.substr(0, equals)] = arg.substr(equals + 1);
  }
  auto p = ungoogled::BuildPersona();
  std::cout << "real=" << p.webgl_real << "\nnative_caps=" << p.webgl_native_capabilities
            << "\nvendor=" << p.webgl_vendor << "\nrenderer=" << p.webgl_renderer
            << "\nwebgpu_vendor=" << p.webgpu_vendor << "\narchitecture=" << p.webgpu_architecture << '\n';
}
'''
    return request.param, compile_cpp(tmp_path_factory.mktemp('gpu-pool-' + request.param), source)


def gpu(fixture, *flags):
    result = subprocess.check_output([str(fixture[1]), *flags], text=True)
    return dict(line.split('=', 1) for line in result.splitlines())


def test_public_gpu_identity_retains_native_capabilities(gpu_persona):
    result = gpu(gpu_persona, 'uxr-fingerprint-enabled=true', 'uxr-fingerprint-seed=42')
    assert result['real'] == '0' and result['native_caps'] == '1'
    assert result == gpu(gpu_persona, 'uxr-fingerprint-enabled=true', 'uxr-fingerprint-seed=42')
    backend = {'windows':'Direct3D11', 'linux':'OpenGL', 'macos':'Metal'}[gpu_persona[0]]
    assert backend in result['renderer']
    assert gpu(gpu_persona)['real'] == '1'
    assert gpu(gpu_persona, 'uxr-fingerprint-enabled=true', 'uxr-fingerprint-seed=42', 'uxr-fingerprint-off=true')['real'] == '1'


@pytest.mark.parametrize('vendor', ['Intel', 'AMD'])
def test_one_sided_vendor_selects_matching_platform_counterpart(gpu_persona, vendor):
    result = gpu(gpu_persona, 'uxr-webgl-vendor=Google Inc. (' + vendor + ')', 'uxr-fingerprint-seed=42')
    assert vendor.lower() in result['renderer'].lower()
    assert result['webgpu_vendor'] == vendor.lower()
    assert result['architecture']


def test_arbitrary_renderer_does_not_inherit_an_unrelated_identity(gpu_persona):
    result = gpu(gpu_persona, 'uxr-webgl-renderer=Custom renderer')
    assert result['renderer'] == 'Custom renderer'
    assert result['vendor'] == result['webgpu_vendor'] == result['architecture'] == ''
    contradictory = gpu(gpu_persona, 'uxr-webgl-vendor=Intel', 'uxr-webgl-renderer=NVIDIA Custom')
    assert contradictory['vendor'] == 'Intel' and contradictory['renderer'] == 'NVIDIA Custom'
    assert contradictory['webgpu_vendor'] == contradictory['architecture'] == ''


def test_mac_arm_pool_uses_metal_apple_tuples(gpu_persona):
    result = gpu(gpu_persona, 'uxr-fingerprint-enabled=true', 'uxr-fingerprint-seed=42',
                 'uxr-ua-platform=macOS', 'uxr-ua-arch=arm')
    assert result['webgpu_vendor'] == 'apple' and result['architecture'].startswith('apple-')
    assert 'Metal' in result['renderer'] and 'Apple M' in result['renderer']


def test_native_auto_is_bounded_and_precedes_profile_creation():
    source = added(144)
    assert 'ResolveFingerprintWebRtcAuto()' in source
    for contract in ['IsInitialized()', 'SetTimeoutDuration(base::Seconds(10))', 'kOmit', 'kError',
                     'proxy_rules().single_proxies.AddProxyChain', 'AppendSwitchASCII("uxr-webrtc-ip", resolved)',
                     'CHROME_RESULT_CODE_INVALID_CMDLINE_URL']:
        assert contract in source
    assert 'CreateInitialProfile' not in source
    assert 'SetUxrConfig' not in source
    assert source.count('IsInitialized()') == 2
    assert source.index('loop.Run()') < source.rindex('IsInitialized()') < source.index('AppendSwitchASCII("uxr-webrtc-ip", resolved)')
    assert 'if (!manager)' in source
    assert 'if (!direct && command_line->HasSwitch(switches::kProxyServer) &&' in source
    assert 'proxy.empty())' in source
    assert 'if (!direct && (command_line->HasSwitch(switches::kProxyPacUrl)' in source
