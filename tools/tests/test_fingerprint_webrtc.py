"""WebRTC routing and STUN regression contracts, not browser/network acceptance.

Fixtures are independent Chromium 152.0.7977.82 excerpts. The C++ harness runs
complete patched STUN callbacks, result-set completion and allocator candidate
acceptance methods. Socket I/O, filtering and notifications remain stubbed; it
does not compile Chromium. CHROMIX_WEBRTC_BASELINE_ROOT enables whole-file
provenance and a strict patch roundtrip against an existing pre-Chromix tree.
"""
from __future__ import annotations

import hashlib
import itertools
import os
from pathlib import Path
import re
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[2]
NUMBERS = (24, 43, 44, 85, 86, 87, 88)
PATCH_BIN = shutil.which("gpatch") or shutil.which("patch")
CXX = os.environ.get("CXX") or shutil.which("clang++") or shutil.which("g++")


def patch_path(number):
    return next((ROOT / "patches").glob(f"{number:04d}-*.patch"))


def target_path(number):
    return re.search(r"^\+\+\+ b/(.+)$", patch_path(number).read_text(), re.M)[1]


def fixture_source(number):
    lines = []
    for first, text in SECTIONS[number]:
        assert len(lines) < first
        lines.extend(["\n"] * (first - len(lines) - 1))
        lines.extend(text.splitlines(keepends=True))
    return "".join(lines)


def apply_patch(directory, number, *, reverse=False, dry_run=False):
    if not PATCH_BIN:
        pytest.skip("GNU patch is required")
    command = [PATCH_BIN, "-p1", "--fuzz=0", "--batch", "--binary", "--get=0",
               "--no-backup-if-mismatch", "--reject-file=-", "--input",
               str(patch_path(number)), "--reverse" if reverse else "--forward"]
    if dry_run:
        command.append("--dry-run")
    return subprocess.run(command, cwd=directory, text=True, capture_output=True,
                          stdin=subprocess.DEVNULL, timeout=15,
                          env={**os.environ, "LC_ALL": "C", "PATCH_GET": "0"})


def assert_strict(result):
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert not re.search(r"fuzz|offset|FAILED|Reversed|Skipping", output, re.I), output


def write_inputs(directory):
    for number in NUMBERS:
        if number == 85:
            continue
        path = directory / target_path(number)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(fixture_source(number), encoding="utf-8", newline="\n")


@pytest.fixture(scope="module")
def patched_sources(tmp_path_factory):
    directory = tmp_path_factory.mktemp("webrtc-patches")
    write_inputs(directory)
    sources = {}
    for number in NUMBERS:
        target = directory / target_path(number)
        original = target.read_bytes()
        assert_strict(apply_patch(directory, number, dry_run=True))
        assert target.read_bytes() == original
        assert_strict(apply_patch(directory, number))
        sources[number] = target.read_text()
    for number in reversed(NUMBERS):
        assert_strict(apply_patch(directory, number, reverse=True, dry_run=True))
        assert_strict(apply_patch(directory, number, reverse=True))
    for number in NUMBERS:
        if number != 85:
            assert (directory / target_path(number)).read_text() == fixture_source(number)
    return sources


def excerpt(source, first, after):
    assert source.count(first) == 1
    start = source.index(first)
    return source[start:source.index(after, start)]


def additions(number):
    return "\n".join(line[1:] for line in patch_path(number).read_text().splitlines()
                     if line.startswith("+") and not line.startswith("+++"))


def test_no_address_substitution_or_fake_stun_state(patched_sources):
    for number in (24, 43, 85, 86, 87, 88):
        code = additions(number)
        assert not re.search(r"ForcedWebRtc|ForcedSrflx|fake.srflx|webrtc-ip|SetIP|SetResolvedIP", code)
        assert "set_mdns_name_registration_status" not in code
        assert "bind_request_succeeded_servers_ =" not in code
    assert "setSdp" not in additions(44)
    assert "candidate_ =" not in additions(43)
    for number in NUMBERS:
        code = additions(number)
        assert any(line.strip() and not line.strip().startswith("//")
                   for line in code.splitlines())
    assert "std::atomic" not in patched_sources[87]


def test_native_candidate_sdp_port_and_stun_success_preserved(patched_sources):
    expected = fixture_source(43).replace('!url_.IsNull()', '!url_.empty()')
    assert patched_sources[43] == expected
    for first, after in [
        ("RTCSessionDescription* RTCPeerConnection::localDescription() const {",
         "ScriptPromise<IDLUndefined> RTCPeerConnection::setRemoteDescription("),
    ]:
        assert excerpt(patched_sources[44], first, after) == excerpt(fixture_source(44), first, after)
    for number, first, after in [
        (87, "void Port::AddAddress(", "void Port::PostAddAddress("),
        (88, "void UDPPort::MaybePrepareStunCandidate()", "Connection* UDPPort::CreateConnection("),
        (88, "void UDPPort::MaybeSetPortCompleteOrError()", "void UDPPort::SendStunRequest("),
    ]:
        assert excerpt(patched_sources[number], first, after) == excerpt(fixture_source(number), first, after)


def test_config_errors_do_not_replace_native_validation(patched_sources):
    source = patched_sources[44]
    guard = source.index('const auto& uxr_config =')
    assert source.index('if (configuration->hasIceTransportPolicy())') < guard
    assert source.index('if (configuration->hasAlwaysNegotiateDataChannels())') < guard
    assert 'web_configuration.type =' not in additions(44)
    assert 'configuration.servers' not in additions(44)
    assert 'kNotSupportedError' in additions(44)
    assert additions(44).count('uxr_config.Has("uxr-webrtc-') == 4
    header = patched_sources[86]
    position = header.index("static bool IsValidStunMappedAddress")
    assert header.rfind("public:", 0, position) > header.rfind("protected:", 0, position)


@pytest.mark.parametrize("number", NUMBERS)
def test_offset_and_wrong_direction_are_not_silently_accepted(tmp_path, patched_sources, number):
    path = tmp_path / target_path(number)
    path.parent.mkdir(parents=True, exist_ok=True)
    before = patched_sources[24] if number == 85 else fixture_source(number)
    path.write_text("\n" + before, encoding="utf-8", newline="\n")
    result = apply_patch(tmp_path, number, dry_run=True)
    assert result.returncode == 0
    with pytest.raises(AssertionError, match="offset"):
        assert_strict(result)
    path.write_text(patched_sources[number], encoding="utf-8", newline="\n")
    assert apply_patch(tmp_path, number, dry_run=True).returncode != 0


def test_optional_baseline_provenance_and_full_file_roundtrip(tmp_path):
    root = os.environ.get("CHROMIX_WEBRTC_BASELINE_ROOT")
    if not root:
        pytest.skip("set CHROMIX_WEBRTC_BASELINE_ROOT to a pre-Chromix 152.0.7977.82 tree")
    originals = {}
    for number in NUMBERS:
        if number == 85:
            continue
        source = Path(root) / target_path(number)
        data = source.read_bytes()
        assert hashlib.sha256(data).hexdigest() == SOURCE_HASHES[number]
        lines = data.decode().splitlines(keepends=True)
        for first, text in SECTIONS[number]:
            assert "".join(lines[first - 1:first - 1 + len(text.splitlines())]) == text
        originals[number] = (data, source.stat().st_mtime_ns)
        target = tmp_path / target_path(number)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    for number in NUMBERS:
        assert_strict(apply_patch(tmp_path, number, dry_run=True))
        assert_strict(apply_patch(tmp_path, number))
    for number in reversed(NUMBERS):
        assert_strict(apply_patch(tmp_path, number, reverse=True))
    for number, (data, timestamp) in originals.items():
        source = Path(root) / target_path(number)
        assert (source.read_bytes(), source.stat().st_mtime_ns) == (data, timestamp)
        assert (tmp_path / target_path(number)).read_bytes() == data


@pytest.fixture(scope="module")
def runtime_binary(tmp_path_factory, patched_sources):
    if not CXX:
        pytest.skip("a C++20 compiler is required for the standalone stub harness")
    source = CPP_SUPPORT + '\nstruct BasicPortAllocatorSession {\n'
    source += ALLOCATOR_PORT_DATA + ALLOCATOR_SUPPORT + '\n};\n' + PORT_CALLBACKS
    source += ALLOCATOR_CALLBACKS
    policy = excerpt(patched_sources[85], '  // Restrict the browser policy;',
                     '  std::unique_ptr<webrtc::NetworkManager> network_manager;')
    source += '\nvoid ApplyPolicy(Config& port_config, bool& allow_mdns_obfuscation) {\n' + policy + '}\n'
    guard = excerpt(patched_sources[44], '  const auto& uxr_config =', '\n  return web_configuration;\n}')
    # Address parsing itself is executed against InetPton in the backend-feature tests.
    source += '''\nstd::string FingerprintWebRtcIp() {\n  auto ip = base::UxrConfig::GetInstance().Get("uxr-webrtc-ip");\n  return ip == "198.51.100.1" || ip == "2001:db8::1" ? ip : "";\n}\n'''
    source += '\nint Validate(ExceptionState* exception_state) {\n  int web_configuration = 7;\n' + guard
    source += '\n  return web_configuration;\n}\n'
    source += excerpt(patched_sources[87], 'bool Port::IsValidStunMappedAddress(', 'void Port::AddAddress(')
    source += excerpt(patched_sources[88], 'void UDPPort::MaybePrepareStunCandidate()', 'Connection* UDPPort::CreateConnection(')
    source += excerpt(patched_sources[88], 'void UDPPort::PostAddAddress(', 'void UDPPort::OnReadPacket(')
    source += excerpt(patched_sources[88], 'void UDPPort::OnStunBindingRequestSucceeded(', 'void UDPPort::SendStunRequest(')
    for name, after in [('OnResponse', '  void OnErrorResponse('),
                        ('OnErrorResponse', '  void OnTimeout() override {'),
                        ('OnTimeout', '\n private:\n')]:
        signature = f'  void {name}(' + (')' if name == 'OnTimeout' else 'StunMessage* response)')
        method = excerpt(patched_sources[88], signature + ' override {', after)
        source += 'void StunBindingRequest::' + method[7:].replace(' override', '', 1)
    relay = excerpt(patched_sources[43], '  // url_ is set only when the candidate was gathered locally.', '\n}\n')
    source += 'int RelayProtocol(const std::string& type_, std::optional<int> priority_, const std::string& url_) {\n'
    source += '  int relay_protocol_ = -1;\n' + relay + '\n  return relay_protocol_;\n}\n'
    source += '\nint AllocatorFlags(const Config& config_) {\n' + ALLOCATOR_FLAGS + '  return flags;\n}\n'
    source += CPP_TESTS
    directory = tmp_path_factory.mktemp("webrtc-runtime")
    cpp, binary = directory / "webrtc.cc", directory / "webrtc"
    cpp.write_text(source, encoding="utf-8", newline="\n")
    result = subprocess.run([CXX, '-std=c++20', '-O0', '-Wall', '-Wextra', '-Werror',
                             str(cpp), '-o', str(binary)], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    return binary


POLICIES = ('', 'default', 'obfuscate', 'disable_non_proxied_udp',
            'default_public_interface_only', 'default_public_and_private_interfaces')


@pytest.mark.parametrize("policy,bits", itertools.product(POLICIES, range(16)))
def test_routing_policy_never_relaxes_native_limits(runtime_binary, policy, bits):
    result = subprocess.run([str(runtime_binary), 'policy', policy, str(bits)],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("case", ["config", "mapped", "responses", "gathering", "success"])
def test_stun_and_configuration_contracts(runtime_binary, case):
    result = subprocess.run([str(runtime_binary), case], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("order,shared,mdns_pending,b_result", itertools.product(
    ["SM", "SSMM", "SME", "SMT", "SMTS", "MS", "MMS", "MSMS", "ES", "TS",
     "EMTS", "MMM", "EET", "SSS", "FS", "SF"],
    [False, True], [False, True], ["S", "M"]))
def test_stun_server_results_do_not_finish_a_pending_server(
        runtime_binary, order, shared, mdns_pending, b_result):
    result = subprocess.run([str(runtime_binary), "server-order", order, str(int(shared)),
                             str(int(mdns_pending)), b_result],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("invalid,live", itertools.product(range(5), [False, True]))
def test_established_stun_keepalive_uses_the_scheduled_request(runtime_binary, invalid, live):
    result = subprocess.run([str(runtime_binary), "keepalive-chain", str(invalid), str(int(live))],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("initial,shared", itertools.product(["SM", "MS", "MM"], [False, True]))
def test_late_stun_responses_do_not_reopen_completed_allocator(runtime_binary, initial, shared):
    result = subprocess.run([str(runtime_binary), "late-response", initial, str(int(shared))],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr


def test_relay_protocol_requires_a_local_server_url(runtime_binary):
    result = subprocess.run([str(runtime_binary), "candidate"], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr


def test_optional_allocator_api_provenance():
    root = os.environ.get("CHROMIX_WEBRTC_API_ROOT")
    if not root:
        pytest.skip("set CHROMIX_WEBRTC_API_ROOT to verify native allocator flags")
    path = Path(root) / "third_party/blink/renderer/platform/p2p/port_allocator.cc"
    assert ALLOCATOR_FLAGS in path.read_text()
    header = (Path(root) / "third_party/blink/renderer/platform/wtf/text/wtf_string.h").read_text()
    assert 'bool empty() const' in header
    source = (Path(root) / "third_party/webrtc/p2p/client/basic_port_allocator.cc").read_text()
    assert ALLOCATOR_ON_CANDIDATE in source
    assert ALLOCATOR_ON_COMPLETE_ERROR in source
    header = (Path(root) / "third_party/webrtc/p2p/client/basic_port_allocator.h").read_text()
    assert ALLOCATOR_PORT_DATA in header
    header = (Path(root) / "third_party/webrtc/p2p/base/stun_port.h").read_text()
    assert "friend class StunBindingRequest;" in header
    assert "ServerAddresses bind_request_succeeded_servers_;" in header
    requests = (Path(root) / "third_party/webrtc/p2p/base/stun_request.cc").read_text()
    success = requests[requests.index("std::unique_ptr<StunRequest> owned_request"):
                       requests.index("owned_request->OnResponse(msg);")]
    assert success.index("std::move(iter->second)") < success.index("requests_.erase(iter);")


ALLOCATOR_FLAGS = r'''  uint32_t flags = 0;
  if (!config_.enable_multiple_routes) {
    flags |= webrtc::PORTALLOCATOR_DISABLE_ADAPTER_ENUMERATION;
  }
  if (!config_.enable_default_local_candidate) {
    flags |= webrtc::PORTALLOCATOR_DISABLE_DEFAULT_LOCAL_CANDIDATE;
  }
  if (!config_.enable_nonproxied_udp) {
    flags |= webrtc::PORTALLOCATOR_DISABLE_UDP |
             webrtc::PORTALLOCATOR_DISABLE_STUN |
             webrtc::PORTALLOCATOR_DISABLE_UDP_RELAY;
  }
'''


CPP_SUPPORT = r'''
#include <cassert>
#include <cstdint>
#include <map>
#include <memory>
#include <optional>
#include <set>
#include <sstream>
#include <string>
#include <string_view>
#include <tuple>
#include <vector>
#include <utility>
constexpr int AF_UNSPEC=0, AF_INET=2, AF_INET6=10;
int PriorityToRelayProtocol(int priority) { return priority; }
namespace base {
struct UxrConfig {
  std::map<std::string, std::string> values;
  static UxrConfig& GetInstance() { static UxrConfig value; return value; }
  bool Has(const std::string& key) const { return values.contains(key); }
  std::string Get(const std::string& key) const {
    auto found = values.find(key); return found == values.end() ? "" : found->second;
  }
};
}
namespace webrtc {
constexpr int PORTALLOCATOR_DISABLE_ADAPTER_ENUMERATION=1;
constexpr int PORTALLOCATOR_DISABLE_DEFAULT_LOCAL_CANDIDATE=2;
constexpr int PORTALLOCATOR_DISABLE_UDP=4, PORTALLOCATOR_DISABLE_STUN=8;
constexpr int PORTALLOCATOR_DISABLE_UDP_RELAY=16;
}
struct Config {
  bool enable_multiple_routes, enable_nonproxied_udp, enable_default_local_candidate;
};
enum class DOMExceptionCode { kNotSupportedError };
struct ExceptionState {
  int errors=0;
  std::string message;
  void ThrowDOMException(DOMExceptionCode, const char* text) { ++errors; message=text; }
};
struct IPAddress {
  int family; bool any=false;
};
struct SocketAddress {
  IPAddress ip; int port_; bool unresolved=false;
  SocketAddress(IPAddress value, int p):ip(value),port_(p) {}
  int family() const { return ip.family; }
  int port() const { return port_; }
  bool IsAnyIP() const { return ip.any; }
  bool IsUnresolvedIP() const { return unresolved; }
  std::string hostname() const { return "stun.test"; }
  std::string ToString() const { return hostname() + ":" + std::to_string(port_); }
  std::string HostAsSensitiveURIString() const { return hostname(); }
  std::string ToSensitiveString() const { return ToString(); }
  bool operator==(const SocketAddress& other) const {
    return family()==other.family() && port_==other.port_ && ip.any==other.ip.any;
  }
  bool operator<(const SocketAddress& other) const {
    return std::tie(ip.family, ip.any, port_, unresolved) <
           std::tie(other.ip.family, other.ip.any, other.port_, other.unresolved);
  }
};
SocketAddress EmptySocketAddressWithFamily(int family) { return {{family,true},0}; }
enum class IceCandidateType { kSrflx, kRelay };
struct Port {
  static bool IsValidStunMappedAddress(const SocketAddress& address);
  std::string ToString() const { return "port"; }
  IceCandidateType Type() const { return IceCandidateType::kSrflx; }
  void KeepAliveUntilPruned() {}
  void Prune() {}
};
struct Candidate {
  SocketAddress address;
  std::string ToSensitiveString() const { return address.ToString(); }
};
struct AllocationSequence {};
struct TimeDelta { int value; int ms() const { return value; } };
using Timestamp=int;
constexpr int kRetryTimeout=50000;
namespace absl { using string_view=std::string_view; }
struct StunAddressAttribute {
  int family_; IPAddress ip; int port_;
  int family() const { return family_; }
  IPAddress ipaddr() const { return ip; }
  int port() const { return port_; }
};
struct StunErrorCodeAttribute {
  int eclass() const { return 5; }
  int number() const { return 500; }
  std::string_view reason() const { return "server error"; }
};
struct StunMessage {
  StunAddressAttribute* attribute;
  StunAddressAttribute* GetAddress(int) { return attribute; }
  StunErrorCodeAttribute* GetErrorCode() { return nullptr; }
};
struct IceCandidateErrorEvent {
  std::string reason;
  IceCandidateErrorEvent(std::string, int, std::string, int, std::string_view text):reason(text) {}
};
constexpr int STUN_ATTR_MAPPED_ADDRESS=1, STUN_ADDRESS_IPV4=1, STUN_ADDRESS_IPV6=2;
constexpr int STUN_ERROR_GLOBAL_FAILURE=600, ICE_TYPE_PREFERENCE_SRFLX=100;
constexpr int STUN_ERROR_NOT_AN_ERROR=0, STUN_ERROR_SERVER_NOT_REACHABLE=701;
constexpr char UDP_PROTOCOL_NAME[]="udp";
enum class MdnsNameRegistrationStatus { kNotStarted, kInProgress, kCompleted };
#define RTC_DCHECK(value) assert(value)
#define RTC_DCHECK_RUN_ON(value) ((void)0)
struct NullLog { template<class T> NullLog& operator<<(const T&) { return *this; } };
#define RTC_LOG(value) NullLog()
using StringBuilder=std::ostringstream;
struct FakeSocket {
  SocketAddress local{{AF_INET},4321};
  SocketAddress GetLocalAddress() const { return local; }
};
struct FakeNetwork {
  bool mdns=false;
  std::string name() const { return "network"; }
  void* GetMdnsResponder() const { return mdns ? (void*)this : nullptr; }
};
struct FakeClock { int CurrentTime() const { return 0; } };
struct FakeEnvironment { FakeClock clock() const { return {}; } };
struct StunBindingRequest;
struct RequestManager {
  int scheduled=0;
  std::vector<std::unique_ptr<StunBindingRequest>> pending;
  void Send(std::unique_ptr<StunBindingRequest> request, int) {
    ++scheduled;
    pending.push_back(std::move(request));
  }
};
struct BasicPortAllocatorSession;
struct UDPPort : Port {
  BasicPortAllocatorSession* session=nullptr;
  struct Stats {
    int stun_binding_responses_received=0, stun_binding_requests_sent=10;
    double stun_binding_rtt_ms_total=0, stun_binding_rtt_ms_squared_total=0;
  } stats_;
  struct Entry { SocketAddress address, base, related; };
  std::vector<Entry> candidates;
  FakeSocket socket_storage;
  FakeSocket* socket_=&socket_storage;
  FakeNetwork network;
  std::set<SocketAddress> server_addresses_, bind_request_succeeded_servers_, bind_request_failed_servers_;
  bool ready_=false, shared=false, default_ok=true;
  int requests=0, errors=0, completes=0, failures=0;
  std::string reason;
  MdnsNameRegistrationStatus mdns_status=MdnsNameRegistrationStatus::kNotStarted;
  RequestManager request_manager_;
  FakeEnvironment env() const { return {}; }
  int stun_keepalive_delay() const { return 100; }
  bool SharedSocket() const { return shared; }
  FakeNetwork* Network() { return &network; }
  MdnsNameRegistrationStatus mdns_name_registration_status() const { return mdns_status; }
  bool MaybeSetDefaultLocalAddress(SocketAddress*) const { return default_ok; }
  bool HasStunCandidateWithAddress(const SocketAddress& address) const {
    for (const auto& c:candidates) if(c.address==address) return true;
    return false;
  }
  void AddAddress(SocketAddress a, SocketAddress b, SocketAddress r,
                  const char*, const char*, const char*, IceCandidateType, int, int, std::string, bool);
  void SendStunBindingRequests() { ++requests; }
  SocketAddress GetLocalAddress() const { return socket_->GetLocalAddress(); }
  void SendCandidateError(const IceCandidateErrorEvent& event) { ++failures; reason=event.reason; }
  void NotifyPortComplete(UDPPort*);
  void NotifyPortError(UDPPort*);
  void MaybePrepareStunCandidate();
  void PostAddAddress(bool);
  void OnStunBindingRequestSucceeded(TimeDelta, const SocketAddress&, const SocketAddress&);
  void MaybeSetPortCompleteOrError();
  void OnStunBindingOrResolveRequestFailed(const SocketAddress&, int, absl::string_view);
};
struct StunBindingRequest {
  UDPPort* port_; SocketAddress server_addr_; int start_time_; bool live=true;
  StunBindingRequest(UDPPort* p, SocketAddress a, int t):port_(p),server_addr_(a),start_time_(t) {}
  FakeEnvironment env() const { return {}; }
  TimeDelta Elapsed() const { return {5}; }
  bool WithinLifetime(int) const { return live; }
  void OnResponse(StunMessage*);
  void OnErrorResponse(StunMessage*);
  void OnTimeout();
};
'''

ALLOCATOR_SUPPORT = r'''
  PortData data;
  std::vector<Candidate> delivered;
  std::string events;
  enum { KEEP_FIRST_READY, PRUNE_BASED_ON_PRIORITY } turn_port_prune_policy_=KEEP_FIRST_READY;
  struct Allocator {
    Candidate SanitizeCandidate(const Candidate& candidate) { return candidate; }
  } allocator;
  Allocator* allocator_=&allocator;
  explicit BasicPortAllocatorSession(UDPPort* port):data(port,nullptr) { port->session=this; }
  PortData* FindPort(Port* port) { assert(port==data.port()); return &data; }
  bool CandidatePairable(const Candidate&, Port*) const { return true; }
  bool CheckCandidateFilter(const Candidate&) const { return true; }
  bool PruneNewlyPairableTurnPort(PortData*) { assert(false); return false; }
  bool PruneTurnPorts(Port*) { assert(false); return false; }
  void NotifyPortReady(BasicPortAllocatorSession*, Port*) {}
  void NotifyCandidatesReady(BasicPortAllocatorSession*, const std::vector<Candidate>& candidates) {
    for (const auto& candidate:candidates) {
      delivered.push_back(candidate);
      events += candidate.address.port()==5000 ? "A" : "B";
    }
  }
  void MaybeSignalCandidatesAllocationDone() { events += data.complete() ? "C" : "E"; }
  void OnCandidateReady(Port*, const Candidate&);
  void OnPortComplete(Port*);
  void OnPortError(Port*);
'''

PORT_CALLBACKS = r'''
void UDPPort::AddAddress(SocketAddress a, SocketAddress b, SocketAddress r,
                        const char*, const char*, const char*, IceCandidateType, int, int, std::string, bool) {
  candidates.push_back({a,b,r});
  if(session) session->OnCandidateReady(this,Candidate{a});
  PostAddAddress(false);
}
void UDPPort::NotifyPortComplete(UDPPort*) {
  ++completes;
  if(session) session->OnPortComplete(this);
}
void UDPPort::NotifyPortError(UDPPort*) {
  ++errors;
  if(session) session->OnPortError(this);
}
'''

CPP_TESTS = r'''
void Respond(UDPPort& port, const SocketAddress& server, int mapped_port, char result) {
  StunAddressAttribute attr{STUN_ADDRESS_IPV4,{AF_INET},mapped_port};
  StunMessage message{result=='S' ? &attr : nullptr};
  StunBindingRequest request(&port,server,0);
  ++port.stats_.stun_binding_requests_sent;
  switch(result) {
    case 'S': case 'M': request.OnResponse(&message); break;
    case 'E': request.OnErrorResponse(&message); break;
    case 'T': request.OnTimeout(); break;
    case 'F': port.OnStunBindingOrResolveRequestFailed(server,STUN_ERROR_NOT_AN_ERROR,"incompatible"); break;
    default: assert(false);
  }
}
void AssertResults(const UDPPort& port, size_t successes, size_t failures) {
  assert(port.bind_request_succeeded_servers_.size()==successes);
  assert(port.bind_request_failed_servers_.size()==failures);
  for(const auto& server:port.bind_request_succeeded_servers_)
    assert(!port.bind_request_failed_servers_.contains(server));
}
int main(int argc, char** argv) {
  assert(argc>=2);
  std::string test=argv[1];
  auto& cfg=base::UxrConfig::GetInstance();
  if(test=="policy") {
    assert(argc==4);
    std::string policy=argv[2]; int bits=std::stoi(argv[3]);
    cfg.values["uxr-webrtc-policy"]=policy;
    Config c{bool(bits&1),bool(bits&2),bool(bits&4)};
    Config original=c; bool mdns=bits&8, original_mdns=mdns;
    ApplyPolicy(c,mdns);
    const int flags=AllocatorFlags(c);
    assert((flags & AllocatorFlags(original))==AllocatorFlags(original));
    assert((flags & 28)==(c.enable_nonproxied_udp ? 0 : 28));
    if(policy=="disable_non_proxied_udp") assert(flags==31);
    assert(!c.enable_multiple_routes || original.enable_multiple_routes);
    assert(!c.enable_nonproxied_udp || original.enable_nonproxied_udp);
    assert(!c.enable_default_local_candidate || original.enable_default_local_candidate);
    if(policy=="disable_non_proxied_udp") {
      assert(!c.enable_multiple_routes && !c.enable_nonproxied_udp && !c.enable_default_local_candidate);
    } else if(policy=="default_public_interface_only") {
      assert(!c.enable_multiple_routes && !c.enable_default_local_candidate);
      assert(c.enable_nonproxied_udp==original.enable_nonproxied_udp);
    } else if(policy=="default_public_and_private_interfaces") {
      assert(!c.enable_multiple_routes);
      assert(c.enable_nonproxied_udp==original.enable_nonproxied_udp);
      assert(c.enable_default_local_candidate==original.enable_default_local_candidate);
    } else {
      assert(c.enable_multiple_routes==original.enable_multiple_routes);
      assert(c.enable_nonproxied_udp==original.enable_nonproxied_udp);
      assert(c.enable_default_local_candidate==original.enable_default_local_candidate);
    }
    assert(mdns == (policy=="obfuscate" || original_mdns));
  } else if(test=="candidate") {
    assert(RelayProtocol("relay", 0, "turn:example.test")==0);
    assert(RelayProtocol("relay", 1, "turn:example.test")==1);
    assert(RelayProtocol("relay", 2, "")==-1);
    assert(RelayProtocol("host", 2, "turn:example.test")==-1);
    assert(RelayProtocol("srflx", 2, "stun:example.test")==-1);
    assert(RelayProtocol("relay", std::nullopt, "turn:example.test")==-1);
  } else if(test=="config") {
    ExceptionState native; assert(Validate(&native)==7 && native.errors==0);
    for(auto key:{"uxr-webrtc-fake-srflx","uxr-webrtc-fake-srflx-allow-udp"}) {
      for(auto value:{"", "auto", "false", "198.51.100.1", "2001:db8::1"}) {
        cfg.values={{key,value}}; ExceptionState e; Validate(&e);
        assert(e.errors==1 && e.message.find("retired")!=std::string::npos);
      }
    }
    for(auto value:{"198.51.100.1", "2001:db8::1"}) {
      cfg.values={{"uxr-webrtc-ip",value}}; ExceptionState e; Validate(&e); assert(!e.errors);
    }
    for(auto value:{"", "auto", "false"}) {
      cfg.values={{"uxr-webrtc-ip",value}}; ExceptionState e; Validate(&e); assert(e.errors==1);
    }
    for(auto value:{"default","obfuscate","disable_non_proxied_udp",
                    "default_public_interface_only","default_public_and_private_interfaces"}) {
      cfg.values={{"uxr-webrtc-policy",value}}; ExceptionState e; Validate(&e); assert(!e.errors);
    }
    for(auto value:{"","native","off","relay","typo","DISABLE_NON_PROXIED_UDP"}) {
      cfg.values={{"uxr-webrtc-policy",value}}; ExceptionState e; Validate(&e); assert(e.errors==1);
    }
  } else if(test=="mapped") {
    for(int family:{AF_INET,AF_INET6}) {
      assert(Port::IsValidStunMappedAddress({{family},1}));
      assert(Port::IsValidStunMappedAddress({{family},65535}));
      assert(!Port::IsValidStunMappedAddress({{family},0}));
      assert(!Port::IsValidStunMappedAddress({{family,true},9999}));
      SocketAddress unresolved{{family},3333}; unresolved.unresolved=true;
      assert(!Port::IsValidStunMappedAddress(unresolved));
    }
    assert(!Port::IsValidStunMappedAddress({{AF_UNSPEC},9}));
  } else if(test=="responses") {
    StunAddressAttribute samples[]={{1,{AF_INET},0},{2,{AF_INET6,true},5},{9,{AF_INET},7},
                                  {1,{AF_UNSPEC},7},{1,{AF_INET},5000},{2,{AF_INET6},6000}};
    for(int i=-1;i<6;++i) {
      UDPPort p; SocketAddress server{{AF_INET},3478}; p.server_addresses_.insert(server);
      StunBindingRequest r(&p,server,0); StunMessage m{i<0?nullptr:&samples[i]}; r.OnResponse(&m);
      if(i<4) {
        assert(p.failures==1 && p.errors==1 && p.ready_ && p.candidates.empty());
        assert(p.bind_request_succeeded_servers_.empty() && p.stats_.stun_binding_responses_received==0);
        assert(p.request_manager_.scheduled==0);
      } else {
        assert(p.failures==0 && p.completes==1 && p.candidates.size()==1);
        assert(p.stats_.stun_binding_responses_received==1 && p.stats_.stun_binding_rtt_ms_total==5);
        assert(p.candidates[0].address.family()==samples[i].ip.family);
        assert(p.candidates[0].address.port()==samples[i].port_);
        assert(p.request_manager_.scheduled==1);
      }
    }
  } else if(test=="keepalive-chain") {
    assert(argc==4);
    int invalid=std::stoi(argv[2]); bool live=std::stoi(argv[3]);
    UDPPort port; BasicPortAllocatorSession allocator(&port);
    const SocketAddress a{{AF_INET},3478}, b{{AF_INET},3479};
    port.server_addresses_={a,b};
    StunAddressAttribute valid{STUN_ADDRESS_IPV4,{AF_INET},5000};
    StunAddressAttribute malformed[]={{1,{AF_INET},0},{2,{AF_INET6,true},5},
                                     {9,{AF_INET},7},{1,{AF_UNSPEC},7}};
    StunMessage good{&valid}, bad{invalid==0 ? nullptr : &malformed[invalid-1]};
    auto deliver = [&](StunMessage& message, bool within_lifetime) {
      auto& pending=port.request_manager_.pending;
      assert(pending.size()==1);
      auto request=std::move(pending.back()); pending.pop_back();
      assert(request->server_addr_==a && request->start_time_==17);
      request->live=within_lifetime;
      request->OnResponse(&message);
    };
    port.request_manager_.Send(std::make_unique<StunBindingRequest>(&port,a,17),0);
    deliver(good,true);
    AssertResults(port,1,0);
    assert(port.request_manager_.pending.size()==1 && allocator.events=="A");
    for(int repeat=0; repeat<3; ++repeat) {
      deliver(bad,live);
      AssertResults(port,1,0);
      assert(!port.ready_ && port.completes==0 && port.errors==0);
      assert(allocator.events=="A" && port.stats_.stun_binding_responses_received==1);
      assert(port.request_manager_.pending.size()==size_t(live));
      if(!live) break;
    }
    if(live) {
      deliver(good,true);
      assert(port.stats_.stun_binding_responses_received==2);
      assert(port.request_manager_.pending.size()==1);
      assert(port.candidates.size()==1 && allocator.events=="A");
    }
    Respond(port,b,6000,'S');
    AssertResults(port,2,0);
    assert(allocator.events=="ABC" && port.completes==1 && port.errors==0);
  } else if(test=="server-order") {
    assert(argc==6);
    std::string order=argv[2]; bool shared=std::stoi(argv[3]), pending_mdns=std::stoi(argv[4]);
    char b_result=argv[5][0];
    UDPPort port; BasicPortAllocatorSession allocator(&port);
    port.shared=shared;
    port.mdns_status=pending_mdns ? MdnsNameRegistrationStatus::kInProgress : MdnsNameRegistrationStatus::kNotStarted;
    const SocketAddress a{{AF_INET},3478}, b{{AF_INET},3479};
    port.server_addresses_={a,b};
    bool a_succeeded=false; int responses=0, failures=0;
    for(char result:order) {
      Respond(port,a,5000,result);
      a_succeeded |= result=='S';
      responses += result=='S'; failures += result!='S' && result!='F';
      assert(!port.ready_ && port.completes==0 && port.errors==0);
      assert(allocator.data.inprogress());
      AssertResults(port,a_succeeded ? 1 : 0,a_succeeded ? 0 : 1);
      assert(allocator.events==(a_succeeded ? "A" : ""));
      assert(port.stats_.stun_binding_responses_received==responses);
      assert(port.stats_.stun_binding_rtt_ms_total==responses*5);
      assert(port.failures==failures);
    }
    Respond(port,b,6000,b_result);
    bool b_succeeded=b_result=='S';
    AssertResults(port,size_t(a_succeeded)+b_succeeded,size_t(!a_succeeded)+!b_succeeded);
    std::string candidates=(a_succeeded ? "A" : "");
    candidates += b_succeeded ? "B" : "";
    bool complete=a_succeeded || b_succeeded || shared;
    if(pending_mdns) {
      assert(!port.ready_ && allocator.data.inprogress());
      assert(allocator.events==candidates);
      port.mdns_status=MdnsNameRegistrationStatus::kCompleted;
      port.MaybeSetPortCompleteOrError();
    }
    assert(port.ready_ && !allocator.data.inprogress());
    assert(port.completes==int(complete) && port.errors==int(!complete));
    assert(allocator.events==candidates+(complete ? "C" : "E"));
    assert(allocator.delivered.size()==candidates.size());
    if(b_succeeded) assert(allocator.delivered.back().address.port()==6000);
    const std::string events=allocator.events;
    Respond(port,b,6000,b_result);
    port.MaybeSetPortCompleteOrError();
    assert(allocator.events==events && port.completes+port.errors==1);
  } else if(test=="late-response") {
    assert(argc==4);
    std::string initial=argv[2];
    UDPPort port; BasicPortAllocatorSession allocator(&port);
    port.shared=std::stoi(argv[3]);
    const SocketAddress a{{AF_INET},3478}, b{{AF_INET},3479};
    port.server_addresses_={a,b};
    Respond(port,a,5000,initial[0]);
    assert(!port.ready_ && allocator.data.inprogress());
    Respond(port,b,6000,initial[1]);
    assert(port.ready_ && !allocator.data.inprogress());
    const std::string events=allocator.events;
    const auto delivered=allocator.delivered.size();
    for(char result:std::string("SSMEMTS")) {
      Respond(port,a,5000,result);
      Respond(port,b,6000,result);
      AssertResults(port,2,0);
      assert(allocator.events==events && allocator.delivered.size()==delivered);
      assert(port.completes+port.errors==1 && port.ready_);
    }
  } else if(test=="gathering") {
    UDPPort p; p.MaybePrepareStunCandidate();
    assert(p.completes==1 && p.requests==0 && p.candidates.empty());
    UDPPort q; q.server_addresses_.insert({{AF_INET},3478}); q.MaybePrepareStunCandidate();
    assert(q.requests==1 && !q.ready_ && q.bind_request_succeeded_servers_.empty());
    UDPPort mdns; mdns.mdns_status=MdnsNameRegistrationStatus::kInProgress;
    mdns.MaybePrepareStunCandidate(); assert(!mdns.ready_);
    mdns.mdns_status=MdnsNameRegistrationStatus::kCompleted;
    mdns.MaybeSetPortCompleteOrError(); assert(mdns.completes==1);
  } else if(test=="success") {
    for(bool shared:{false,true}) for(bool mdns:{false,true}) for(bool related:{false,true}) {
      UDPPort p; p.shared=shared; p.network.mdns=mdns; p.default_ok=related;
      SocketAddress server{{AF_INET},3478}, mapped{{AF_INET6},9000};
      p.server_addresses_.insert(server); p.OnStunBindingRequestSucceeded({4},server,mapped);
      assert(p.candidates.size()==1 && p.candidates[0].address==mapped);
      assert(p.candidates[0].base==p.socket_->GetLocalAddress());
      assert(p.candidates[0].related.port()==(related?4321:0));
      p.OnStunBindingRequestSucceeded({6},server,mapped);
      assert(p.candidates.size()==1 && p.stats_.stun_binding_responses_received==2);
      assert(p.stats_.stun_binding_rtt_ms_total==10 && p.stats_.stun_binding_rtt_ms_squared_total==52);
      assert(p.completes==1);
    }
  } else { assert(false); }
}
'''


SOURCE_HASHES = {24: 'd8a78b65cfe01413926408722241e7615c92bc5e1cf62660337c98d65a30a90e', 43: '59f54b000b3d3dcac139a00f752ac1325affb545307062410e281364ac30afce', 44: '04a034dc67a5432373f5fa035644b81dba73eecaa9df573feb7f2e69b81fe3fc', 86: '2c1477b337e7273eb5b78362cbd4052f1b7b196ca102c47ea0581a29d254db30', 87: 'c6d4eefbd78be59df17b9b48a334ddf786152fcabd8233af5cd7dbdb8773111d', 88: 'fa20c9efac26c8e64f45573a025b0d782adefc65a23ef8542c5b24a226cab5c6'}

# Chromium 152.0.7977.82 pre-Chromix excerpts, with original line numbers.
SECTIONS = {
    24: [
        (1, r'''// Copyright 2014 The Chromium Authors
// Use of this source code is governed by a BSD-style license that can be
// found in the LICENSE file.

#include "third_party/blink/renderer/modules/peerconnection/peer_connection_dependency_factory.h"

#include <stddef.h>

#include <memory>
#include <string>
#include <utility>
#include <vector>

#include "base/feature_list.h"
#include "base/functional/callback.h"
#include "base/functional/callback_helpers.h"
#include "base/location.h"
#include "base/logging.h"
#include "base/memory/raw_ptr.h"
#include "base/metrics/field_trial_params.h"
#include "base/metrics/histogram_functions.h"
#include "base/metrics/histogram_macros.h"
#include "base/notreached.h"
#include "base/synchronization/waitable_event.h"
#include "base/task/sequenced_task_runner.h"
#include "base/task/single_thread_task_runner.h"
#include "base/time/time.h"
#include "base/trace_event/trace_event.h"
#include "base/unguessable_token.h"
#include "build/build_config.h"
#include "components/webrtc/thread_wrapper.h"
#include "crypto/openssl_util.h"
#include "media/base/media_permission.h"
#include "media/media_buildflags.h"
#include "media/mojo/clients/mojo_video_encoder_metrics_provider.h"
#include "media/video/gpu_video_accelerator_factories.h"
'''),
        (1045, r'''std::unique_ptr<webrtc::PortAllocator>
PeerConnectionDependencyFactory::CreatePortAllocator(
    blink::WebLocalFrame* web_frame) {
  DCHECK(web_frame);
  EnsureInitialized();

  // Copy the flag from Preference associated with this WebLocalFrame.
  P2PPortAllocator::Config port_config;
  uint16_t min_port = 0;
  uint16_t max_port = 0;
  bool allow_mdns_obfuscation = true;

  // |media_permission| will be called to check mic/camera permission. If at
  // least one of them is granted, P2PPortAllocator is allowed to gather local
  // host IP addresses as ICE candidates. |media_permission| could be nullptr,
  // which means the permission will be granted automatically. This could be the
  // case when either the experiment is not enabled or the preference is not
  // enforced.
  //
  // Note on |media_permission| lifetime: |media_permission| is owned by a frame
  // (RenderFrameImpl). It is also stored as an indirect member of
  // RTCPeerConnectionHandler (through PeerConnection/PeerConnectionInterface ->
  // P2PPortAllocator -> FilteringNetworkManager -> |media_permission|).
  // The RTCPeerConnectionHandler is owned as RTCPeerConnection::m_peerHandler
  // in Blink, which will be reset in RTCPeerConnection::stop(). Since
  // ActiveDOMObject::stop() is guaranteed to be called before a frame is
  // detached, it is impossible for RTCPeerConnectionHandler to outlive the
  // frame. Therefore using a raw pointer of |media_permission| is safe here.
  media::MediaPermission* media_permission = nullptr;
  if (!Platform::Current()->ShouldEnforceWebRTCRoutingPreferences()) {
    port_config.enable_multiple_routes = true;
    port_config.enable_nonproxied_udp = true;
    VLOG(3) << "WebRTC routing preferences will not be enforced";
  } else {
    if (web_frame && web_frame->View()) {
      mojom::blink::WebRtcIpHandlingPolicy webrtc_ip_handling_policy;
      Platform::Current()->GetWebRTCRendererPreferences(
          web_frame, &webrtc_ip_handling_policy, &min_port, &max_port,
          &allow_mdns_obfuscation);
      DVLOG(1) << "Active WebRtcIPHandlingPolicy: "
               << ToString(webrtc_ip_handling_policy);
      // TODO(guoweis): |enable_multiple_routes| should be renamed to
      // |request_multiple_routes|. Whether local IP addresses could be
      // collected depends on if mic/camera permission is granted for this
      // origin.
      switch (webrtc_ip_handling_policy) {
        // TODO(guoweis): specify the flag of disabling local candidate
        // collection when webrtc is updated.
        case mojom::blink::WebRtcIpHandlingPolicy::kDefaultPublicInterfaceOnly:
        case mojom::blink::WebRtcIpHandlingPolicy::
            kDefaultPublicAndPrivateInterfaces:
          port_config.enable_multiple_routes = false;
          port_config.enable_nonproxied_udp = true;
          port_config.enable_default_local_candidate =
              (webrtc_ip_handling_policy ==
               mojom::blink::WebRtcIpHandlingPolicy::
                   kDefaultPublicAndPrivateInterfaces);
          break;
        case mojom::blink::WebRtcIpHandlingPolicy::kDisableNonProxiedUdp:
          port_config.enable_multiple_routes = false;
          port_config.enable_nonproxied_udp = false;
          break;
        case mojom::blink::WebRtcIpHandlingPolicy::kDefault:
          port_config.enable_multiple_routes = true;
          port_config.enable_nonproxied_udp = true;
          break;
      }

      VLOG(3) << "WebRTC routing preferences: " << "policy: "
              << ToString(webrtc_ip_handling_policy)
              << ", multiple_routes: " << port_config.enable_multiple_routes
              << ", nonproxied_udp: " << port_config.enable_nonproxied_udp
              << ", min_udp_port: " << min_port
              << ", max_udp_port: " << max_port
              << ", allow_mdns_obfuscation: " << allow_mdns_obfuscation;
    }
    if (port_config.enable_multiple_routes) {
      media_permission =
          blink::Platform::Current()->GetWebRTCMediaPermission(web_frame);
    }
  }

  std::unique_ptr<webrtc::NetworkManager> network_manager;
  if (port_config.enable_multiple_routes) {
    network_manager = std::make_unique<FilteringNetworkManager>(
        network_manager_.get(), media_permission, allow_mdns_obfuscation);
  } else {
    network_manager =
        std::make_unique<blink::EmptyNetworkManager>(network_manager_.get());
  }

  auto port_allocator = std::make_unique<P2PPortAllocator>(
      std::move(network_manager), socket_factory_.get(), port_config,
      std::make_unique<LocalNetworkAccessPermissionFactory>(this));
  if (IsValidPortRange(min_port, max_port))
    port_allocator->SetPortRange(min_port, max_port);

  return port_allocator;
}

'''),
    ],
    43: [
        (1, r'''// Copyright 2019 The Chromium Authors
// Use of this source code is governed by a BSD-style license that can be
// found in the LICENSE file.

#include "third_party/blink/renderer/platform/peerconnection/rtc_ice_candidate_platform.h"

#include "third_party/webrtc/api/candidate.h"
#include "third_party/webrtc/p2p/base/p2p_constants.h"

namespace blink {

namespace {

// Maps |component| to constants defined in
// https://w3c.github.io/webrtc-pc/#dom-rtcicecomponent
String CandidateComponentToString(int component) {
  if (component == webrtc::ICE_CANDIDATE_COMPONENT_RTP) {
    return String("rtp");
  }
  if (component == webrtc::ICE_CANDIDATE_COMPONENT_RTCP) {
    return String("rtcp");
  }
  return String();
}

// Determine the relay protocol from local type preference which is the
// lower 8 bits of the priority. The mapping to relay protocol is defined
// in webrtc/p2p/base/port.h and only valid for relay candidates.
String PriorityToRelayProtocol(uint32_t priority) {
  uint8_t local_type_preference = priority >> 24;
  switch (local_type_preference) {
    case 0:
      return String("tls");
    case 1:
      return String("tcp");
    case 2:
      return String("udp");
  }
  return String();
}

}  // namespace

RTCIceCandidatePlatform::RTCIceCandidatePlatform(
    String candidate,
    String sdp_mid,
    std::optional<uint16_t> sdp_m_line_index,
    String username_fragment,
    String url)
    : candidate_(std::move(candidate)),
      sdp_mid_(std::move(sdp_mid)),
      sdp_m_line_index_(std::move(sdp_m_line_index)),
      username_fragment_(std::move(username_fragment)),
      url_(std::move(url)) {
  PopulateFields();
}

void RTCIceCandidatePlatform::PopulateFields() {
  webrtc::RTCErrorOr<webrtc::Candidate> parsed_candidate =
      webrtc::Candidate::ParseCandidateString(candidate_.Utf8());
  if (!parsed_candidate.ok()) {
    return;
  }

  const webrtc::Candidate& c = parsed_candidate.value();

  foundation_ = String::FromUtf8(c.foundation());
  component_ = CandidateComponentToString(c.component());
  priority_ = c.priority();
  protocol_ = String::FromUtf8(c.protocol());
  if (!c.address().IsNil()) {
    address_ = String::FromUtf8(c.address().HostAsURIString());
    port_ = c.address().port();
  }
  // The `type_name()` property returns a name as specified in:
  // https://datatracker.ietf.org/doc/html/rfc5245#section-15.1
  // which is identical to:
  // https://w3c.github.io/webrtc-pc/#rtcicecandidatetype-enum
  auto type = c.type_name();
  DCHECK(type == "host" || type == "srflx" || type == "prflx" ||
         type == "relay");
  type_ = String(type);
  if (!c.tcptype().empty()) {
    tcp_type_ = String::FromUtf8(c.tcptype());
  }
  if (!c.related_address().IsNil()) {
    related_address_ = String::FromUtf8(c.related_address().HostAsURIString());
    related_port_ = c.related_address().port();
  }
  // url_ is set only when the candidate was gathered locally.
  if (type_ == "relay" && priority_ && !url_.IsNull()) {
    relay_protocol_ = PriorityToRelayProtocol(*priority_);
  }
}

}  // namespace blink
'''),
    ],
    44: [
        (33, r'''#include <algorithm>
#include <memory>
#include <optional>
#include <string>
#include <utility>

#include "base/compiler_specific.h"
#include "base/containers/to_vector.h"
#include "base/feature_list.h"
#include "base/lazy_instance.h"
#include "base/memory/ptr_util.h"
#include "base/metrics/histogram_functions.h"
#include "base/metrics/histogram_macros.h"
#include "base/notreached.h"
#include "base/numerics/safe_conversions.h"
#include "base/task/single_thread_task_runner.h"
#include "base/task/thread_pool.h"
#include "build/build_config.h"
#include "build/buildflag.h"
#include "services/metrics/public/cpp/ukm_builders.h"
#include "services/network/public/cpp/connection_allowlist.h"
#include "third_party/blink/public/common/features.h"
'''),
        (306, r'''webrtc::PeerConnectionInterface::RTCConfiguration ParseConfiguration(
    ExecutionContext* context,
    const RTCConfiguration* configuration,
    ExceptionState* exception_state) {
  DCHECK(context);

  webrtc::PeerConnectionInterface::RTCConfiguration web_configuration;

  if (configuration->hasIceTransportPolicy()) {
    UseCounter::Count(context, WebFeature::kRTCConfigurationIceTransportPolicy);
    web_configuration.type = IceTransportPolicyFromEnum(
        configuration->iceTransportPolicy().AsEnum());
  } else if (configuration->hasIceTransports()) {
    UseCounter::Count(context, WebFeature::kRTCConfigurationIceTransports);
    web_configuration.type =
        IceTransportPolicyFromEnum(configuration->iceTransports().AsEnum());
  }

  switch (configuration->bundlePolicy().AsEnum()) {
    case V8RTCBundlePolicy::Enum::kMaxCompat:
      web_configuration.bundle_policy =
          webrtc::PeerConnectionInterface::kBundlePolicyMaxCompat;
      break;
    case V8RTCBundlePolicy::Enum::kMaxBundle:
      web_configuration.bundle_policy =
          webrtc::PeerConnectionInterface::kBundlePolicyMaxBundle;
      break;
    case V8RTCBundlePolicy::Enum::kBalanced:
      break;
  }

  switch (configuration->rtcpMuxPolicy().AsEnum()) {
    case V8RTCRtcpMuxPolicy::Enum::kNegotiate:
      web_configuration.rtcp_mux_policy =
          webrtc::PeerConnectionInterface::kRtcpMuxPolicyNegotiate;
      Deprecation::CountDeprecation(context,
                                    WebFeature::kRtcpMuxPolicyNegotiate);
      break;
    case V8RTCRtcpMuxPolicy::Enum::kRequire:
      break;
  }

  if (RuntimeEnabledFeatures::RtcRtpHeaderEncryptionPolicyEnabled()) {
    switch (configuration->rtpHeaderEncryptionPolicy().AsEnum()) {
      case V8RTCRtpHeaderEncryptionPolicy::Enum::kNegotiate:
        web_configuration.crypto_options.srtp.cryptex_policy =
            webrtc::CryptoOptions::Srtp::CryptexPolicy::kNegotiate;
        break;
      case V8RTCRtpHeaderEncryptionPolicy::Enum::kRequire:
        web_configuration.crypto_options.srtp.cryptex_policy =
            webrtc::CryptoOptions::Srtp::CryptexPolicy::kRequire;
        break;
    }
  }

  // If RTC connections are blocked globally, communication with all ICE servers
  // should be also blocked. The simplest way to accomplish this is to filter
  // them all out before they reach the native layer.
  if (!AreIceCandidatesAdministrativelyProhibited(context)) {
    std::vector<webrtc::PeerConnectionInterface::IceServer>& ice_servers =
        web_configuration.servers;
    for (const RTCIceServer* ice_server : configuration->iceServers()) {
      Vector<String> url_strings;
      std::vector<std::string> converted_urls;
      if (ice_server->hasUrls()) {
        UseCounter::Count(context, WebFeature::kRTCIceServerURLs);
        switch (ice_server->urls()->GetContentType()) {
          case V8UnionStringOrStringSequence::ContentType::kString:
            url_strings.push_back(ice_server->urls()->GetAsString());
            break;
          case V8UnionStringOrStringSequence::ContentType::kStringSequence:
            url_strings = ice_server->urls()->GetAsStringSequence();
            break;
        }
      } else if (ice_server->hasUrl()) {
        UseCounter::Count(context, WebFeature::kRTCIceServerURL);
        url_strings.push_back(ice_server->url());
      } else {
        exception_state->ThrowTypeError("Malformed RTCIceServer");
        return {};
      }

      for (const String& url_string : url_strings) {
        KURL url(NullUrl(), url_string);
        if (!url.IsValid()) {
          exception_state->ThrowDOMException(
              DOMExceptionCode::kSyntaxError,
              StrCat({"'", url_string, "' is not a valid URL."}));
          return {};
        }
        bool is_valid_turn = IsValidTurnURL(url);
        if (!is_valid_turn && !IsValidStunURL(url)) {
          exception_state->ThrowDOMException(
              DOMExceptionCode::kSyntaxError,
              StrCat({"'", url_string, "' is not a valid stun or turn URL."}));
          return {};
        }
        if (is_valid_turn &&
            (!ice_server->hasUsername() || !ice_server->hasCredential())) {
          exception_state->ThrowDOMException(
              DOMExceptionCode::kInvalidAccessError,
              "Both username and credential are "
              "required when the URL scheme is "
              "\"turn\" or \"turns\".");
        }

        converted_urls.push_back(String(url).Utf8());
      }

      auto converted_ice_server = webrtc::PeerConnectionInterface::IceServer();
      converted_ice_server.urls = std::move(converted_urls);
      if (ice_server->hasUsername()) {
        converted_ice_server.username = ice_server->username().Utf8();
      }
      if (ice_server->hasCredential()) {
        converted_ice_server.password = ice_server->credential().Utf8();
      }
      ice_servers.emplace_back(std::move(converted_ice_server));
    }
  }

  web_configuration.certificates = base::ToVector(
      configuration->certificates(),
      [](const auto& certificate) { return certificate->Certificate(); });

  web_configuration.ice_candidate_pool_size =
      configuration->iceCandidatePoolSize();

  if (configuration->hasRtcAudioJitterBufferMaxPackets()) {
    UseCounter::Count(context, WebFeature::kRTCMaxAudioBufferSize);
    web_configuration.audio_jitter_buffer_max_packets =
        static_cast<int>(configuration->rtcAudioJitterBufferMaxPackets());
  }

  if (configuration->hasRtcAudioJitterBufferFastAccelerate()) {
    UseCounter::Count(context, WebFeature::kRTCMaxAudioBufferSize);
    web_configuration.audio_jitter_buffer_fast_accelerate =
        configuration->hasRtcAudioJitterBufferFastAccelerate();
  }

  if (configuration->hasRtcAudioJitterBufferMinDelayMs()) {
    UseCounter::Count(context, WebFeature::kRTCMaxAudioBufferSize);
    web_configuration.audio_jitter_buffer_min_delay_ms =
        static_cast<int>(configuration->rtcAudioJitterBufferMinDelayMs());
  }

  if (configuration->hasAlwaysNegotiateDataChannels()) {
    web_configuration.always_negotiate_data_channels =
        configuration->alwaysNegotiateDataChannels();
  }

  return web_configuration;
}

'''),
        (1256, r'''RTCSessionDescription* RTCPeerConnection::localDescription() const {
  return pending_local_description_ ? pending_local_description_
                                    : current_local_description_;
}

RTCSessionDescription* RTCPeerConnection::currentLocalDescription() const {
  return current_local_description_.Get();
}

RTCSessionDescription* RTCPeerConnection::pendingLocalDescription() const {
  return pending_local_description_.Get();
}

ScriptPromise<IDLUndefined> RTCPeerConnection::setRemoteDescription(
'''),
    ],
    86: [
        (153, r'''class RTC_EXPORT Port : public PortInterface {
 public:
  // A struct containing common arguments to creating a port. See also
  // CreateRelayPortArgs.
  struct PortParametersRef {
    Environment env;
    TaskQueueBase* network_thread;
    PacketSocketFactory* socket_factory;
    const ::webrtc::Network* network;
    absl::string_view ice_username_fragment;
    absl::string_view ice_password;
    absl::string_view content_name;
    LocalNetworkAccessPermissionFactoryInterface* lna_permission_factory =
        nullptr;
    uint64_t ice_tiebreaker;
  };

 protected:
  // Constructors for use only by via constructors in derived classes.
  Port(const PortParametersRef& args, IceCandidateType type);
  Port(const PortParametersRef& args,
       IceCandidateType type,
       uint16_t min_port,
       uint16_t max_port,
       bool shared_socket = false);

 public:
  ~Port() override;

  // Note that the port type does NOT uniquely identify different subclasses of
  // Port. Use the 2-tuple of the port type AND the protocol (GetProtocol()) to
  // uniquely identify subclasses. Whenever a new subclass of Port introduces a
  // conflict in the value of the 2-tuple, make sure that the implementation
  // that relies on this 2-tuple for RTTI is properly changed.
'''),
        (436, r'''
  void SubscribeSentPacket(
      const void* tag,
      absl::AnyInvocable<void(const SentPacketInfo&)> callback) override;
  void NotifySentPacket(const SentPacketInfo& packet) override;

 protected:
  void UpdateNetworkCost() override;

  WeakPtr<Port> NewWeakPtr() {
    RTC_DCHECK_RUN_ON(thread_);
    return weak_factory_.GetWeakPtr();
  }

  void AddAddress(const SocketAddress& address,
                  const SocketAddress& base_address,
                  const SocketAddress& related_address,
                  absl::string_view protocol,
                  absl::string_view relay_protocol,
                  absl::string_view tcptype,
                  IceCandidateType type,
                  uint32_t type_preference,
                  uint32_t relay_preference,
                  absl::string_view url,
                  bool is_final);

  void FinishAddingAddress(const Candidate& c, bool is_final)
      RTC_RUN_ON(thread_);
'''),
    ],
    87: [
        (204, r'''    return nullptr;
}

void Port::AddAddress(const SocketAddress& address,
                      const SocketAddress& base_address,
                      const SocketAddress& related_address,
                      absl::string_view protocol,
                      absl::string_view relay_protocol,
                      absl::string_view tcptype,
                      IceCandidateType type,
                      uint32_t type_preference,
                      uint32_t relay_preference,
                      absl::string_view url,
                      bool is_final) {
  RTC_DCHECK_RUN_ON(thread_);

  // TODO(tommi): Set relay_protocol and optionally provide the base address
  // to automatically compute the foundation in the ctor? It would be a good
  // thing for the Candidate class to know the base address and keep it const.
  Candidate c(component_, protocol, address, 0U, username_fragment(), password_,
              type, generation_, "", network_->id(), network_cost_);
  // Set the relay protocol before computing the foundation field.
  c.set_relay_protocol(relay_protocol);
  c.ComputeFoundation(base_address, ice_tiebreaker_);

  c.set_priority(
      c.GetPriority(type_preference, network_->preference(), relay_preference,
                    env().field_trials().IsEnabled(
                        "WebRTC-IncreaseIceCandidatePriorityHostSrflx")));
#if RTC_DCHECK_IS_ON
  if (protocol == TCP_PROTOCOL_NAME && c.is_local()) {
    RTC_DCHECK(!tcptype.empty());
  }
#endif
  c.set_tcptype(tcptype);
  c.set_network_name(network_->name());
  c.set_network_type(network_->type());
  c.set_underlying_type_for_vpn(network_->underlying_type_for_vpn());
  c.set_url(url);
  c.set_related_address(related_address);
  c.set_network_slice(network_->network_slice());

  bool pending = MaybeObfuscateAddress(c, is_final);

  if (!pending) {
    FinishAddingAddress(c, is_final);
  }
}

bool Port::MaybeObfuscateAddress(const Candidate& c, bool is_final) {
  // TODO(bugs.webrtc.org/9723): Use a config to control the feature of IP
  // handling with mDNS.
  if (network_->GetMdnsResponder() == nullptr) {
    return false;
  }
  if (!c.is_local()) {
    return false;
  }

  auto copy = c;
  auto weak_ptr = weak_factory_.GetWeakPtr();
  auto callback = [weak_ptr, copy, is_final](const IPAddress& addr,
                                             absl::string_view name) mutable {
    RTC_DCHECK(copy.address().ipaddr() == addr);
    SocketAddress hostname_address(name, copy.address().port());
    // In Port and Connection, we need the IP address information to
    // correctly handle the update of candidate type to prflx. The removal
    // of IP address when signaling this candidate will take place in
    // BasicPortAllocatorSession::OnCandidateReady, via SanitizeCandidate.
    hostname_address.SetResolvedIP(addr);
    copy.set_address(hostname_address);
    copy.set_related_address(SocketAddress());
    if (weak_ptr != nullptr) {
      RTC_DCHECK_RUN_ON(weak_ptr->thread_);
      weak_ptr->set_mdns_name_registration_status(
          MdnsNameRegistrationStatus::kCompleted);
      weak_ptr->FinishAddingAddress(copy, is_final);
    }
  };
  set_mdns_name_registration_status(MdnsNameRegistrationStatus::kInProgress);
  network_->GetMdnsResponder()->CreateNameForAddress(copy.address().ipaddr(),
                                                     callback);
  return true;
}

void Port::FinishAddingAddress(const Candidate& c, bool is_final) {
  candidates_.push_back(c);
  NotifyCandidateReady(this, c);

  PostAddAddress(is_final);
}

void Port::PostAddAddress(bool is_final) {
  if (is_final) {
    NotifyPortComplete(this);
  }
}

'''),
    ],
    88: [
        (62, r'''// Handles a binding request sent to the STUN server.
class StunBindingRequest : public StunRequest {
 public:
  StunBindingRequest(UDPPort* port,
                     const SocketAddress& addr,
                     Timestamp start_time)
      : StunRequest(port->env(),
                    port->request_manager(),
                    std::make_unique<StunMessage>(STUN_BINDING_REQUEST)),
        port_(port),
        server_addr_(addr),
        start_time_(start_time) {
    SetAuthenticationRequired(false);
  }

  const SocketAddress& server_addr() const { return server_addr_; }

  void OnResponse(StunMessage* response) override {
    const StunAddressAttribute* addr_attr =
        response->GetAddress(STUN_ATTR_MAPPED_ADDRESS);
    if (!addr_attr) {
      RTC_LOG(LS_ERROR) << "Binding response missing mapped address.";
    } else if (addr_attr->family() != STUN_ADDRESS_IPV4 &&
               addr_attr->family() != STUN_ADDRESS_IPV6) {
      RTC_LOG(LS_ERROR) << "Binding address has bad family";
    } else {
      SocketAddress addr(addr_attr->ipaddr(), addr_attr->port());
      port_->OnStunBindingRequestSucceeded(this->Elapsed(), server_addr_, addr);
    }

    // The keep-alive requests will be stopped after its lifetime has passed.
    if (WithinLifetime(env().clock().CurrentTime())) {
      port_->request_manager_.Send(std::make_unique<StunBindingRequest>(
                                       port_, server_addr_, start_time_),
                                   /*delay=*/port_->stun_keepalive_delay());
    }
  }

  void OnErrorResponse(StunMessage* response) override {
    const StunErrorCodeAttribute* attr = response->GetErrorCode();
    if (!attr) {
      RTC_LOG(LS_ERROR) << "Missing binding response error code.";
    } else {
      RTC_LOG(LS_ERROR) << "Binding error response:"
                           " class="
                        << attr->eclass() << " number=" << attr->number()
                        << " reason=" << attr->reason();
    }

    port_->OnStunBindingOrResolveRequestFailed(
        server_addr_, attr ? attr->number() : STUN_ERROR_GLOBAL_FAILURE,
        attr ? attr->reason()
             : "STUN binding response with no error code attribute.");

    Timestamp now = env().clock().CurrentTime();
    if (WithinLifetime(now) && now - start_time_ < kRetryTimeout) {
      port_->request_manager_.Send(std::make_unique<StunBindingRequest>(
                                       port_, server_addr_, start_time_),
                                   /*delay=*/port_->stun_keepalive_delay());
    }
  }
  void OnTimeout() override {
    RTC_LOG(LS_ERROR) << "Binding request timed out from "
                      << port_->GetLocalAddress().ToSensitiveString() << " ("
                      << port_->Network()->name() << ")";
    port_->OnStunBindingOrResolveRequestFailed(
        server_addr_, STUN_ERROR_SERVER_NOT_REACHABLE,
        "STUN binding request timed out.");
  }

 private:
  // Returns true if `now` is within the lifetime of the request.
  bool WithinLifetime(Timestamp now) const {
    return now - start_time_ <= port_->stun_keepalive_lifetime();
  }

  UDPPort* port_;
  const SocketAddress server_addr_;

  Timestamp start_time_;
};

'''),
        (262, r'''void UDPPort::MaybePrepareStunCandidate() {
  // Sending binding request to the STUN server if address is available to
  // prepare STUN candidate.
  if (!server_addresses_.empty()) {
    SendStunBindingRequests();
  } else {
    // Port is done allocating candidates.
    MaybeSetPortCompleteOrError();
  }
}

Connection* UDPPort::CreateConnection(const Candidate& address,
                                      CandidateOrigin /* origin */) {
  if (!SupportsProtocol(address.protocol())) {
    return nullptr;
  }

  if (!IsCompatibleAddress(address.address())) {
    return nullptr;
  }

  // In addition to DCHECK-ing the non-emptiness of local candidates, we also
  // skip this Port with null if there are latent bugs to violate it; otherwise
  // it would lead to a crash when accessing the local candidate of the
  // connection that would be created below.
  if (Candidates().empty()) {
    RTC_DCHECK_NOTREACHED();
    return nullptr;
  }
  // When the socket is shared, the srflx candidate is gathered by the UDPPort.
  // The assumption here is that
  //  1) if the IP concealment with mDNS is not enabled, the gathering of the
  //     host candidate of this port (which is synchronous),
  //  2) or otherwise if enabled, the start of name registration of the host
  //     candidate (as the start of asynchronous gathering)
  // is always before the gathering of a srflx candidate (and any prflx
  // candidate).
  //
  // See also the definition of MdnsNameRegistrationStatus::kNotStarted in
  // port.h.
  RTC_DCHECK(!SharedSocket() || Candidates()[0].is_local() ||
             mdns_name_registration_status() !=
                 MdnsNameRegistrationStatus::kNotStarted);

  Connection* conn = new ProxyConnection(env(), NewWeakPtr(), 0, address);
  AddOrReplaceConnection(conn);
  return conn;
}

'''),
        (400, r'''void UDPPort::PostAddAddress(bool /* is_final */) {
  MaybeSetPortCompleteOrError();
}

void UDPPort::OnReadPacket(AsyncPacketSocket* socket,
'''),
        (546, r'''void UDPPort::OnStunBindingRequestSucceeded(
    TimeDelta rtt,
    const SocketAddress& stun_server_addr,
    const SocketAddress& stun_reflected_addr) {
  int rtt_ms = rtt.ms();
  RTC_DCHECK(stats_.stun_binding_responses_received <
             stats_.stun_binding_requests_sent);
  stats_.stun_binding_responses_received++;
  stats_.stun_binding_rtt_ms_total += rtt_ms;
  stats_.stun_binding_rtt_ms_squared_total += rtt_ms * rtt_ms;
  if (bind_request_succeeded_servers_.find(stun_server_addr) !=
      bind_request_succeeded_servers_.end()) {
    return;
  }
  bind_request_succeeded_servers_.insert(stun_server_addr);
  // If socket is shared and `stun_reflected_addr` is equal to local socket
  // address and mDNS obfuscation is not enabled, or if the same address has
  // been added by another STUN server, then discarding the stun address.
  // For STUN, related address is the local socket address.
  if ((!SharedSocket() || stun_reflected_addr != socket_->GetLocalAddress() ||
       Network()->GetMdnsResponder() != nullptr) &&
      !HasStunCandidateWithAddress(stun_reflected_addr)) {
    SocketAddress related_address = socket_->GetLocalAddress();
    // If we can't stamp the related address correctly, empty it to avoid leak.
    if (!MaybeSetDefaultLocalAddress(&related_address)) {
      related_address = EmptySocketAddressWithFamily(related_address.family());
    }

    StringBuilder url;
    url << "stun:" << stun_server_addr.hostname() << ":"
        << stun_server_addr.port();
    AddAddress(stun_reflected_addr, socket_->GetLocalAddress(), related_address,
               UDP_PROTOCOL_NAME, "", "", IceCandidateType::kSrflx,
               ICE_TYPE_PREFERENCE_SRFLX, 0, url.str(), false);
  }
  MaybeSetPortCompleteOrError();
}

void UDPPort::OnStunBindingOrResolveRequestFailed(
    const SocketAddress& stun_server_addr,
    int error_code,
    absl::string_view reason) {
  if (error_code != STUN_ERROR_NOT_AN_ERROR) {
    StringBuilder url;
    url << "stun:" << stun_server_addr.ToString();
    SendCandidateError(IceCandidateErrorEvent(
        GetLocalAddress().HostAsSensitiveURIString(), GetLocalAddress().port(),
        url.str(), error_code, reason));
  }
  if (bind_request_failed_servers_.find(stun_server_addr) !=
      bind_request_failed_servers_.end()) {
    return;
  }
  bind_request_failed_servers_.insert(stun_server_addr);
  MaybeSetPortCompleteOrError();
}

void UDPPort::MaybeSetPortCompleteOrError() {
  if (mdns_name_registration_status() ==
      MdnsNameRegistrationStatus::kInProgress) {
    return;
  }

  if (ready_) {
    return;
  }

  // Do not set port ready if we are still waiting for bind responses.
  const size_t servers_done_bind_request =
      bind_request_failed_servers_.size() +
      bind_request_succeeded_servers_.size();
  if (server_addresses_.size() != servers_done_bind_request) {
    return;
  }

  // Setting ready status.
  ready_ = true;

  // The port is "completed" if there is no stun server provided, or the bind
  // request succeeded for any stun server, or the socket is shared.
  if (server_addresses_.empty() || !bind_request_succeeded_servers_.empty() ||
      SharedSocket()) {
    NotifyPortComplete(this);
  } else {
    NotifyPortError(this);
  }
}

void UDPPort::SendStunRequest(std::span<const uint8_t> data, StunRequest* req) {
  StunBindingRequest* sreq = static_cast<StunBindingRequest*>(req);
  AsyncSocketPacketOptions options(StunDscpValue());
  options.info_signaled_after_sent.packet_type = PacketType::kStunMessage;
  SendTo(data, sreq->server_addr(), options, /*payload=*/true);

  stats_.stun_binding_requests_sent++;
}

'''),
    ],
}

# Chromium 152 allocator methods; networking, filtering and notifications are stubbed.
ALLOCATOR_PORT_DATA = r'''  class PortData {
   public:
    enum State {
      STATE_INPROGRESS,  // Still gathering candidates.
      STATE_COMPLETE,    // All candidates allocated and ready for process.
      STATE_ERROR,       // Error in gathering candidates.
      STATE_PRUNED       // Pruned by higher priority ports on the same network
                         // interface. Only TURN ports may be pruned.
    };

    PortData() = delete;
    PortData(PortData&&) = default;
    PortData(Port* port, AllocationSequence* seq)
        : port_(port), sequence_(seq) {}

    PortData& operator=(PortData&&) = default;

    Port* port() const { return port_; }
    AllocationSequence* sequence() const { return sequence_; }
    bool has_pairable_candidate() const { return has_pairable_candidate_; }
    State state() const { return state_; }
    bool complete() const { return state_ == STATE_COMPLETE; }
    bool error() const { return state_ == STATE_ERROR; }
    bool pruned() const { return state_ == STATE_PRUNED; }
    bool inprogress() const { return state_ == STATE_INPROGRESS; }
    // Returns true if this port is ready to be used.
    bool ready() const {
      return has_pairable_candidate_ && state_ != STATE_ERROR &&
             state_ != STATE_PRUNED;
    }
    // Sets the state to "PRUNED" and prunes the Port.
    void Prune() {
      state_ = STATE_PRUNED;
      if (port()) {
        port()->Prune();
      }
    }
    void set_has_pairable_candidate(bool has_pairable_candidate) {
      if (has_pairable_candidate) {
        RTC_DCHECK(state_ == STATE_INPROGRESS);
      }
      has_pairable_candidate_ = has_pairable_candidate;
    }
    void set_state(State state) {
      RTC_DCHECK(state != STATE_ERROR || state_ == STATE_INPROGRESS);
      state_ = state;
    }

   private:
    Port* port_ = nullptr;
    AllocationSequence* sequence_ = nullptr;
    bool has_pairable_candidate_ = false;
    State state_ = STATE_INPROGRESS;
  };

'''

ALLOCATOR_ON_CANDIDATE = r'''void BasicPortAllocatorSession::OnCandidateReady(Port* port,
                                                 const Candidate& c) {
  RTC_DCHECK_RUN_ON(network_thread_);
  PortData* data = FindPort(port);
  RTC_DCHECK(data != nullptr);
  RTC_LOG(LS_INFO) << port->ToString()
                   << ": Gathered candidate: " << c.ToSensitiveString();
  // Discarding any candidate signal if port allocation status is
  // already done with gathering.
  if (!data->inprogress()) {
    RTC_LOG(LS_WARNING)
        << "Discarding candidate because port is already done gathering.";
    return;
  }

  // Mark that the port has a pairable candidate, either because we have a
  // usable candidate from the port, or simply because the port is bound to the
  // any address and therefore has no host candidate. This will trigger the port
  // to start creating candidate pairs (connections) and issue connectivity
  // checks. If port has already been marked as having a pairable candidate,
  // do nothing here.
  // Note: We should check whether any candidates may become ready after this
  // because there we will check whether the candidate is generated by the ready
  // ports, which may include this port.
  bool pruned = false;
  if (CandidatePairable(c, port) && !data->has_pairable_candidate()) {
    data->set_has_pairable_candidate(true);

    if (port->Type() == IceCandidateType::kRelay) {
      if (turn_port_prune_policy_ == KEEP_FIRST_READY) {
        pruned = PruneNewlyPairableTurnPort(data);
      } else if (turn_port_prune_policy_ == PRUNE_BASED_ON_PRIORITY) {
        pruned = PruneTurnPorts(port);
      }
    }

    // If the current port is not pruned yet, SignalPortReady.
    if (!data->pruned()) {
      RTC_LOG(LS_INFO) << port->ToString() << ": Port ready.";
      NotifyPortReady(this, port);
      port->KeepAliveUntilPruned();
    }
  }

  if (data->ready() && CheckCandidateFilter(c)) {
    std::vector<Candidate> candidates;
    candidates.push_back(allocator_->SanitizeCandidate(c));
    NotifyCandidatesReady(this, candidates);
  } else {
    RTC_LOG(LS_INFO) << "Discarding candidate because it doesn't match filter.";
  }

  // If we have pruned any port, maybe need to signal port allocation done.
  if (pruned) {
    MaybeSignalCandidatesAllocationDone();
  }
}

'''

ALLOCATOR_ON_COMPLETE_ERROR = r'''void BasicPortAllocatorSession::OnPortComplete(Port* port) {
  RTC_DCHECK_RUN_ON(network_thread_);
  RTC_LOG(LS_INFO) << port->ToString()
                   << ": Port completed gathering candidates.";
  PortData* data = FindPort(port);
  RTC_DCHECK(data != nullptr);

  // Ignore any late signals.
  if (!data->inprogress()) {
    return;
  }

  // Moving to COMPLETE state.
  data->set_state(PortData::STATE_COMPLETE);
  // Send candidate allocation complete signal if this was the last port.
  MaybeSignalCandidatesAllocationDone();
}

void BasicPortAllocatorSession::OnPortError(Port* port) {
  RTC_DCHECK_RUN_ON(network_thread_);
  RTC_LOG(LS_INFO) << port->ToString()
                   << ": Port encountered error while gathering candidates.";
  PortData* data = FindPort(port);
  RTC_DCHECK(data != nullptr);
  // We might have already given up on this port and stopped it.
  if (!data->inprogress()) {
    return;
  }

  // SignalAddressError is currently sent from StunPort/TurnPort.
  // But this signal itself is generic.
  data->set_state(PortData::STATE_ERROR);
  // Send candidate allocation complete signal if this was the last port.
  MaybeSignalCandidatesAllocationDone();
}

'''

ALLOCATOR_CALLBACKS = ALLOCATOR_ON_CANDIDATE + ALLOCATOR_ON_COMPLETE_ERROR


# Extended native Chromium 152 excerpts; whole-file provenance remains SOURCE_HASHES[44].
SECTIONS[44] = [
    (33, '#include <algorithm>\n#include <memory>\n#include <optional>\n#include <string>\n#include <utility>\n\n#include "base/compiler_specific.h"\n#include "base/containers/to_vector.h"\n#include "base/feature_list.h"\n#include "base/lazy_instance.h"\n#include "base/memory/ptr_util.h"\n#include "base/metrics/histogram_functions.h"\n#include "base/metrics/histogram_macros.h"\n#include "base/notreached.h"\n#include "base/numerics/safe_conversions.h"\n#include "base/task/single_thread_task_runner.h"\n#include "base/task/thread_pool.h"\n#include "build/build_config.h"\n#include "build/buildflag.h"\n#include "services/metrics/public/cpp/ukm_builders.h"\n#include "services/network/public/cpp/connection_allowlist.h"\n#include "third_party/blink/public/common/features.h"\n'),
    (306, 'webrtc::PeerConnectionInterface::RTCConfiguration ParseConfiguration(\n    ExecutionContext* context,\n    const RTCConfiguration* configuration,\n    ExceptionState* exception_state) {\n  DCHECK(context);\n\n  webrtc::PeerConnectionInterface::RTCConfiguration web_configuration;\n\n  if (configuration->hasIceTransportPolicy()) {\n    UseCounter::Count(context, WebFeature::kRTCConfigurationIceTransportPolicy);\n    web_configuration.type = IceTransportPolicyFromEnum(\n        configuration->iceTransportPolicy().AsEnum());\n  } else if (configuration->hasIceTransports()) {\n    UseCounter::Count(context, WebFeature::kRTCConfigurationIceTransports);\n    web_configuration.type =\n        IceTransportPolicyFromEnum(configuration->iceTransports().AsEnum());\n  }\n\n  switch (configuration->bundlePolicy().AsEnum()) {\n    case V8RTCBundlePolicy::Enum::kMaxCompat:\n      web_configuration.bundle_policy =\n          webrtc::PeerConnectionInterface::kBundlePolicyMaxCompat;\n      break;\n    case V8RTCBundlePolicy::Enum::kMaxBundle:\n      web_configuration.bundle_policy =\n          webrtc::PeerConnectionInterface::kBundlePolicyMaxBundle;\n      break;\n    case V8RTCBundlePolicy::Enum::kBalanced:\n      break;\n  }\n\n  switch (configuration->rtcpMuxPolicy().AsEnum()) {\n    case V8RTCRtcpMuxPolicy::Enum::kNegotiate:\n      web_configuration.rtcp_mux_policy =\n          webrtc::PeerConnectionInterface::kRtcpMuxPolicyNegotiate;\n      Deprecation::CountDeprecation(context,\n                                    WebFeature::kRtcpMuxPolicyNegotiate);\n      break;\n    case V8RTCRtcpMuxPolicy::Enum::kRequire:\n      break;\n  }\n\n  if (RuntimeEnabledFeatures::RtcRtpHeaderEncryptionPolicyEnabled()) {\n    switch (configuration->rtpHeaderEncryptionPolicy().AsEnum()) {\n      case V8RTCRtpHeaderEncryptionPolicy::Enum::kNegotiate:\n        web_configuration.crypto_options.srtp.cryptex_policy =\n            webrtc::CryptoOptions::Srtp::CryptexPolicy::kNegotiate;\n        break;\n      case V8RTCRtpHeaderEncryptionPolicy::Enum::kRequire:\n        web_configuration.crypto_options.srtp.cryptex_policy =\n            webrtc::CryptoOptions::Srtp::CryptexPolicy::kRequire;\n        break;\n    }\n  }\n\n  // If RTC connections are blocked globally, communication with all ICE servers\n  // should be also blocked. The simplest way to accomplish this is to filter\n  // them all out before they reach the native layer.\n  if (!AreIceCandidatesAdministrativelyProhibited(context)) {\n    std::vector<webrtc::PeerConnectionInterface::IceServer>& ice_servers =\n        web_configuration.servers;\n    for (const RTCIceServer* ice_server : configuration->iceServers()) {\n      Vector<String> url_strings;\n      std::vector<std::string> converted_urls;\n      if (ice_server->hasUrls()) {\n        UseCounter::Count(context, WebFeature::kRTCIceServerURLs);\n        switch (ice_server->urls()->GetContentType()) {\n          case V8UnionStringOrStringSequence::ContentType::kString:\n            url_strings.push_back(ice_server->urls()->GetAsString());\n            break;\n          case V8UnionStringOrStringSequence::ContentType::kStringSequence:\n            url_strings = ice_server->urls()->GetAsStringSequence();\n            break;\n        }\n      } else if (ice_server->hasUrl()) {\n        UseCounter::Count(context, WebFeature::kRTCIceServerURL);\n        url_strings.push_back(ice_server->url());\n      } else {\n        exception_state->ThrowTypeError("Malformed RTCIceServer");\n        return {};\n      }\n\n      for (const String& url_string : url_strings) {\n        KURL url(NullUrl(), url_string);\n        if (!url.IsValid()) {\n          exception_state->ThrowDOMException(\n              DOMExceptionCode::kSyntaxError,\n              StrCat({"\'", url_string, "\' is not a valid URL."}));\n          return {};\n        }\n        bool is_valid_turn = IsValidTurnURL(url);\n        if (!is_valid_turn && !IsValidStunURL(url)) {\n          exception_state->ThrowDOMException(\n              DOMExceptionCode::kSyntaxError,\n              StrCat({"\'", url_string, "\' is not a valid stun or turn URL."}));\n          return {};\n        }\n        if (is_valid_turn &&\n            (!ice_server->hasUsername() || !ice_server->hasCredential())) {\n          exception_state->ThrowDOMException(\n              DOMExceptionCode::kInvalidAccessError,\n              "Both username and credential are "\n              "required when the URL scheme is "\n              "\\"turn\\" or \\"turns\\".");\n        }\n\n        converted_urls.push_back(String(url).Utf8());\n      }\n\n      auto converted_ice_server = webrtc::PeerConnectionInterface::IceServer();\n      converted_ice_server.urls = std::move(converted_urls);\n      if (ice_server->hasUsername()) {\n        converted_ice_server.username = ice_server->username().Utf8();\n      }\n      if (ice_server->hasCredential()) {\n        converted_ice_server.password = ice_server->credential().Utf8();\n      }\n      ice_servers.emplace_back(std::move(converted_ice_server));\n    }\n  }\n\n  web_configuration.certificates = base::ToVector(\n      configuration->certificates(),\n      [](const auto& certificate) { return certificate->Certificate(); });\n\n  web_configuration.ice_candidate_pool_size =\n      configuration->iceCandidatePoolSize();\n\n  if (configuration->hasRtcAudioJitterBufferMaxPackets()) {\n    UseCounter::Count(context, WebFeature::kRTCMaxAudioBufferSize);\n    web_configuration.audio_jitter_buffer_max_packets =\n        static_cast<int>(configuration->rtcAudioJitterBufferMaxPackets());\n  }\n\n  if (configuration->hasRtcAudioJitterBufferFastAccelerate()) {\n    UseCounter::Count(context, WebFeature::kRTCMaxAudioBufferSize);\n    web_configuration.audio_jitter_buffer_fast_accelerate =\n        configuration->hasRtcAudioJitterBufferFastAccelerate();\n  }\n\n  if (configuration->hasRtcAudioJitterBufferMinDelayMs()) {\n    UseCounter::Count(context, WebFeature::kRTCMaxAudioBufferSize);\n    web_configuration.audio_jitter_buffer_min_delay_ms =\n        static_cast<int>(configuration->rtcAudioJitterBufferMinDelayMs());\n  }\n\n  if (configuration->hasAlwaysNegotiateDataChannels()) {\n    web_configuration.always_negotiate_data_channels =\n        configuration->alwaysNegotiateDataChannels();\n  }\n\n  return web_configuration;\n}\n\n'),
    (1136, '    }\n  }\n\n  ExecutionContext* context = ExecutionContext::From(script_state);\n  ParsedSessionDescription parsed_sdp = ParsedSessionDescription::Parse(\n      session_description_init->type().AsString(), sdp);\n'),
    (1232, '    }\n  }\n\n  ParsedSessionDescription parsed_sdp = ParsedSessionDescription::Parse(\n      session_description_init->hasType()\n          ? session_description_init->type().AsString()\n'),
    (1256, 'RTCSessionDescription* RTCPeerConnection::localDescription() const {\n  return pending_local_description_ ? pending_local_description_\n                                    : current_local_description_;\n}\n\nRTCSessionDescription* RTCPeerConnection::currentLocalDescription() const {\n  return current_local_description_.Get();\n}\n\nRTCSessionDescription* RTCPeerConnection::pendingLocalDescription() const {\n  return pending_local_description_.Get();\n}\n\nScriptPromise<IDLUndefined> RTCPeerConnection::setRemoteDescription(\n'),
    (2524, '  tracks_.insert(track->Component(), track);\n}\n\nvoid RTCPeerConnection::NoteSdpCreated(const RTCSessionDescriptionInit& desc) {\n  if (desc.type() == V8RTCSdpType::Enum::kOffer) {\n    last_offer_ = desc.sdp();\n'),
    (2573, '  DCHECK(!closed_);\n  DCHECK(GetExecutionContext()->IsContextThread());\n  DCHECK(platform_candidate);\n  RTCIceCandidate* ice_candidate = RTCIceCandidate::Create(platform_candidate);\n  MaybeDispatchEvent(RTCPeerConnectionIceEvent::Create(ice_candidate));\n}\n'),
    (2585, '                                            const String& error_text) {\n  DCHECK(!closed_);\n  DCHECK(GetExecutionContext()->IsContextThread());\n  MaybeDispatchEvent(RTCPeerConnectionIceErrorEvent::Create(\n      address, port, host_candidate, url, error_code, error_text));\n}\n\nvoid RTCPeerConnection::DidChangeSessionDescriptions(\n'),
    (2596, '    RTCSessionDescriptionPlatform* current_remote_description) {\n  DCHECK(!closed_);\n  DCHECK(GetExecutionContext()->IsContextThread());\n  pending_local_description_ =\n      pending_local_description\n          ? RTCSessionDescription::Create(pending_local_description)\n          : nullptr;\n  current_local_description_ =\n      current_local_description\n          ? RTCSessionDescription::Create(current_local_description)\n          : nullptr;\n  pending_remote_description_ =\n      pending_remote_description\n          ? RTCSessionDescription::Create(pending_remote_description)\n'),
]
