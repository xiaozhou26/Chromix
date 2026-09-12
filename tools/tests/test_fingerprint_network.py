"""Native NetworkInformation regressions with independent Chromium 152 fixtures.

The embedded sources are pinned to 152.0.7977.82, not reconstructed from the
patch. CHROMIX_NETWORK_BASELINE_ROOT optionally supplies pre-Chromix netinfo
source; CHROMIX_NETWORK_NOTIFIER_ROOT supplies notifier source for provenance.
Only temporary copies are patched. Complete getters, ConnectionChange and
observer lifecycle methods are compiled verbatim with native notifier rounding
and holdback methods. IPC, task scheduling, Blink hashing and V8 are stubbed;
this is neither a browser integration test nor a complete network template.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[2]
PATCH = ROOT / "patches/0023-third_party-blink-renderer-modules-netinfo-network_information-cc.patch"
TARGET = Path("third_party/blink/renderer/modules/netinfo/network_information.cc")
NOTIFIER = Path("third_party/blink/renderer/platform/network/network_state_notifier.cc")
PATCH_BIN = shutil.which("gpatch") or shutil.which("patch")
CXX = shutil.which("clang++") or shutil.which("g++")
SOURCE_SHA256 = "faa7782a89a9fb6cfd5d3916d559683a5b3e669b4ffb06741ebbf3abf68b303e"
RETIREMENT = "// Keep getters and change events on the same native notifier state.\n"


def apply_patch(directory, *, reverse=False, dry_run=False):
    if not PATCH_BIN:
        pytest.skip("GNU patch is required")
    command = [PATCH_BIN, "-p1", "--fuzz=0", "--batch", "--binary", "--get=0",
               "--no-backup-if-mismatch", "--reject-file=-", "--input", str(PATCH), "--forward"]
    if reverse:
        command.append("--reverse")
    if dry_run:
        command.append("--dry-run")
    return subprocess.run(command, cwd=directory, text=True, capture_output=True,
                          stdin=subprocess.DEVNULL, timeout=15,
                          env={**os.environ, "LC_ALL": "C", "PATCH_GET": "0"})


def assert_strict(result):
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert not re.search(r"fuzz|offset|FAILED|Reversed|Skipping", output, re.I), output


def write_source(directory, source):
    target = directory / TARGET
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(source)
    return target


@pytest.fixture(scope="module")
def patched_source(tmp_path_factory):
    directory = tmp_path_factory.mktemp("network-patch")
    original = NETWORK_SOURCE.encode()
    target = write_source(directory, original)
    assert_strict(apply_patch(directory, dry_run=True))
    assert target.read_bytes() == original
    assert_strict(apply_patch(directory))
    patched = target.read_text()
    assert_strict(apply_patch(directory, reverse=True, dry_run=True))
    assert target.read_text() == patched
    assert_strict(apply_patch(directory, reverse=True))
    assert target.read_bytes() == original
    return patched


def test_independent_fixture_apply_reverse(patched_source):
    assert hashlib.sha256(NETWORK_SOURCE.encode()).hexdigest() == SOURCE_SHA256
    assert patched_source.count(RETIREMENT) == 1
    assert patched_source.replace(RETIREMENT, "") == NETWORK_SOURCE


def test_retirement_is_one_comment_and_preserves_120_slots():
    patch = PATCH.read_text()
    additions = [line[1:] for line in patch.splitlines()
                 if line.startswith("+") and not line.startswith("+++")]
    assert additions == [RETIREMENT.rstrip("\n")]
    assert not any(line.startswith("-") and not line.startswith("---")
                   for line in patch.splitlines())
    assert patch.count("diff --git ") == 1
    assert patch.count("\n@@ ") == 1
    assert re.findall(r"^\+\+\+ b/(.*)$", patch, re.M) == [TARGET.as_posix()]
    assert not re.search(r"uxr|persona|StringToDouble", patch, re.I)
    series = [line for line in (ROOT / "patches/series").read_text().splitlines()
              if line.strip() and not line.startswith("#")]
    assert [Path(line).name[:4] for line in series] == [f"{i:04d}" for i in range(1, len(series) + 1)]
    assert series[22] == PATCH.relative_to(ROOT).as_posix()
    assert len(list((ROOT / "patches").glob("[0-9][0-9][0-9][0-9]-*.patch"))) == len(series)


@pytest.mark.parametrize("context", [
    "  return !!connection_observer_handle_;",
    "V8ConnectionType NetworkInformation::type() const {",
    "  if (RuntimeEnabledFeatures::NetInfoConstantTypeEnabled()) {",
])
def test_changed_context_rejected_without_fuzz(tmp_path, context):
    assert NETWORK_SOURCE.count(context) >= 1
    changed = NETWORK_SOURCE.replace(context, "// incompatible context", 1).encode()
    target = write_source(tmp_path, changed)
    result = apply_patch(tmp_path, dry_run=True)
    assert result.returncode != 0, result.stdout + result.stderr
    assert "FAILED" in result.stdout + result.stderr
    assert target.read_bytes() == changed


@pytest.mark.parametrize("reverse", [False, True])
def test_offset_success_is_rejected(tmp_path, patched_source, reverse):
    source = patched_source if reverse else NETWORK_SOURCE
    original = ("// shifted source\n" + source).encode()
    target = write_source(tmp_path, original)
    result = apply_patch(tmp_path, reverse=reverse, dry_run=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "offset" in result.stdout + result.stderr
    with pytest.raises(AssertionError, match="offset"):
        assert_strict(result)
    assert target.read_bytes() == original


@pytest.mark.parametrize("reverse", [False, True])
def test_wrong_patch_direction_rejected(tmp_path, patched_source, reverse):
    original = (NETWORK_SOURCE if reverse else patched_source).encode()
    target = write_source(tmp_path, original)
    result = apply_patch(tmp_path, reverse=reverse, dry_run=True)
    assert result.returncode != 0, result.stdout + result.stderr
    assert target.read_bytes() == original


def test_optional_baseline_provenance_and_roundtrip(tmp_path):
    root = os.environ.get("CHROMIX_NETWORK_BASELINE_ROOT")
    if not root:
        pytest.skip("set CHROMIX_NETWORK_BASELINE_ROOT to a pre-Chromix 152.0.7977.82 tree")
    source = Path(root) / TARGET
    original, timestamp = source.read_bytes(), source.stat().st_mtime_ns
    assert hashlib.sha256(original).hexdigest() == SOURCE_SHA256
    assert original == NETWORK_SOURCE.encode()
    target = write_source(tmp_path, original)
    assert_strict(apply_patch(tmp_path, dry_run=True))
    assert_strict(apply_patch(tmp_path))
    assert_strict(apply_patch(tmp_path, reverse=True, dry_run=True))
    assert_strict(apply_patch(tmp_path, reverse=True))
    assert target.read_bytes() == original
    assert (source.read_bytes(), source.stat().st_mtime_ns) == (original, timestamp)


def test_optional_notifier_provenance():
    root = os.environ.get("CHROMIX_NETWORK_NOTIFIER_ROOT")
    if not root:
        pytest.skip("set CHROMIX_NETWORK_NOTIFIER_ROOT to check native rounding/holdback source")
    root = Path(root)
    assert (root / "chrome/VERSION").read_text() == "MAJOR=152\nMINOR=0\nBUILD=7977\nPATCH=82\n"
    source = root / NOTIFIER
    original, timestamp = source.read_bytes(), source.stat().st_mtime_ns
    lines = original.decode().splitlines(keepends=True)
    for first, text in NOTIFIER_SECTIONS:
        assert "".join(lines[first - 1:first - 1 + len(text.splitlines())]) == text
    assert (source.read_bytes(), source.stat().st_mtime_ns) == (original, timestamp)


def excerpt(source, first, after):
    assert source.count(first) == 1
    start = source.index(first)
    return source[start:source.index(after, start)]


@pytest.fixture(scope="module")
def runtime_binary(tmp_path_factory, patched_source):
    if not CXX:
        pytest.skip("a local C++20 compiler is required for the standalone stub runtime")
    methods = excerpt(patched_source, "namespace {", "const char NetworkInformation::kSupplementName[]")
    methods += excerpt(patched_source, "NetworkInformation::NetworkInformation(NavigatorBase& navigator)",
                       "void NetworkInformation::Trace(")
    methods += excerpt(patched_source, "const String NetworkInformation::Host() const {",
                       "\n}  // namespace blink")
    notifier = "\n".join(text for _, text in NOTIFIER_SECTIONS)
    directory = tmp_path_factory.mktemp("network-runtime")
    source, binary = directory / "network.cc", directory / "network"
    source.write_text(CPP_SUPPORT + notifier + methods + CPP_TESTS)
    result = subprocess.run([CXX, "-std=c++20", "-O0", "-Wall", "-Werror",
                             str(source), "-o", str(binary)], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    return binary


@pytest.mark.parametrize("case", [
    "native", "enum-mapping", "observing", "holdback", "holdback-precedence", "notifier-override",
    "rounding", "save-data", "change-suppression", "feature-gates", "event-order", "lifecycle",
    "host-rounding",
])
def test_complete_native_functions(runtime_binary, case):
    result = subprocess.run([str(runtime_binary), case], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == f"{case}: PASS\n"
    print(result.stdout, end="")


# Chromium 152.0.7977.82, complete independent pre-Chromix file.
NETWORK_SOURCE = r'''// Copyright 2014 The Chromium Authors
// Use of this source code is governed by a BSD-style license that can be
// found in the LICENSE file.

#include "third_party/blink/renderer/modules/netinfo/network_information.h"

#include <algorithm>

#include "base/time/time.h"
#include "third_party/blink/public/mojom/devtools/console_message.mojom-blink.h"
#include "third_party/blink/public/platform/task_type.h"
#include "third_party/blink/renderer/bindings/modules/v8/v8_connection_type.h"
#include "third_party/blink/renderer/bindings/modules/v8/v8_effective_connection_type.h"
#include "third_party/blink/renderer/core/dom/events/event.h"
#include "third_party/blink/renderer/core/execution_context/execution_context.h"
#include "third_party/blink/renderer/core/execution_context/navigator_base.h"
#include "third_party/blink/renderer/core/inspector/console_message.h"
#include "third_party/blink/renderer/core/probe/core_probes.h"
#include "third_party/blink/renderer/modules/event_target_modules.h"
#include "third_party/blink/renderer/platform/heap/garbage_collected.h"
#include "third_party/blink/renderer/platform/runtime_enabled_features.h"
#include "third_party/blink/renderer/platform/supplementable.h"
#include "third_party/blink/renderer/platform/weborigin/kurl.h"
#include "third_party/blink/renderer/platform/wtf/text/wtf_string.h"

namespace blink {

namespace {

V8ConnectionType::Enum ConnectionTypeToEnum(WebConnectionType type) {
  switch (type) {
    case kWebConnectionTypeCellular2G:
    case kWebConnectionTypeCellular3G:
    case kWebConnectionTypeCellular4G:
      return V8ConnectionType::Enum::kCellular;
    case kWebConnectionTypeBluetooth:
      return V8ConnectionType::Enum::kBluetooth;
    case kWebConnectionTypeEthernet:
      return V8ConnectionType::Enum::kEthernet;
    case kWebConnectionTypeWifi:
      return V8ConnectionType::Enum::kWifi;
    case kWebConnectionTypeWimax:
      return V8ConnectionType::Enum::kWimax;
    case kWebConnectionTypeOther:
      return V8ConnectionType::Enum::kOther;
    case kWebConnectionTypeNone:
      return V8ConnectionType::Enum::kNone;
    case kWebConnectionTypeUnknown:
      return V8ConnectionType::Enum::kUnknown;
  }
  NOTREACHED();
}

V8EffectiveConnectionType::Enum EffectiveConnectionTypeToEnum(
    WebEffectiveConnectionType type) {
  switch (type) {
    case WebEffectiveConnectionType::kTypeSlow2G:
      return V8EffectiveConnectionType::Enum::kSlow2G;
    case WebEffectiveConnectionType::kType2G:
      return V8EffectiveConnectionType::Enum::k2G;
    case WebEffectiveConnectionType::kType3G:
      return V8EffectiveConnectionType::Enum::k3G;
    case WebEffectiveConnectionType::kTypeUnknown:
    case WebEffectiveConnectionType::kTypeOffline:
    case WebEffectiveConnectionType::kType4G:
      return V8EffectiveConnectionType::Enum::k4G;
  }
  NOTREACHED();
}

String GetConsoleLogStringForWebHoldback() {
  return "Network quality values are overridden using a holdback experiment, "
         "and so may be inaccurate";
}

}  // namespace

NetworkInformation::~NetworkInformation() {
  DCHECK(!IsObserving());
}

bool NetworkInformation::IsObserving() const {
  return !!connection_observer_handle_;
}

V8ConnectionType NetworkInformation::type() const {
  if (RuntimeEnabledFeatures::NetInfoConstantTypeEnabled()) {
    return V8ConnectionType(V8ConnectionType::Enum::kUnknown);
  }

  // type_ is only updated when listening for events, so ask
  // networkStateNotifier if not listening (crbug.com/379841).
  if (!IsObserving()) {
    return V8ConnectionType(
        ConnectionTypeToEnum(GetNetworkStateNotifier().ConnectionType()));
  }

  // If observing, return m_type which changes when the event fires, per spec.
  return V8ConnectionType(ConnectionTypeToEnum(type_));
}

double NetworkInformation::downlinkMax() const {
  if (RuntimeEnabledFeatures::NetInfoConstantTypeEnabled()) {
    return std::numeric_limits<double>::infinity();
  }

  if (!IsObserving())
    return GetNetworkStateNotifier().MaxBandwidth();

  return downlink_max_mbps_;
}

V8EffectiveConnectionType NetworkInformation::effectiveType() {
  MaybeShowWebHoldbackConsoleMsg();
  std::optional<WebEffectiveConnectionType> override_ect =
      GetNetworkStateNotifier().GetWebHoldbackEffectiveType();
  if (override_ect) {
    return V8EffectiveConnectionType(
        EffectiveConnectionTypeToEnum(override_ect.value()));
  }

  // effective_type_ is only updated when listening for events, so ask
  // networkStateNotifier if not listening (crbug.com/379841).
  if (!IsObserving()) {
    return V8EffectiveConnectionType(EffectiveConnectionTypeToEnum(
        GetNetworkStateNotifier().EffectiveType()));
  }

  // If observing, return m_type which changes when the event fires, per spec.
  return V8EffectiveConnectionType(
      EffectiveConnectionTypeToEnum(effective_type_));
}

uint32_t NetworkInformation::rtt() {
  MaybeShowWebHoldbackConsoleMsg();
  std::optional<base::TimeDelta> override_rtt =
      GetNetworkStateNotifier().GetWebHoldbackHttpRtt();
  if (override_rtt) {
    return GetNetworkStateNotifier().RoundRtt(Host(), override_rtt.value());
  }

  if (!IsObserving()) {
    return GetNetworkStateNotifier().RoundRtt(
        Host(), GetNetworkStateNotifier().HttpRtt());
  }

  return http_rtt_msec_;
}

double NetworkInformation::downlink() {
  MaybeShowWebHoldbackConsoleMsg();
  std::optional<double> override_downlink_mbps =
      GetNetworkStateNotifier().GetWebHoldbackDownlinkThroughputMbps();
  if (override_downlink_mbps) {
    return GetNetworkStateNotifier().RoundMbps(Host(),
                                               override_downlink_mbps.value());
  }

  if (!IsObserving()) {
    return GetNetworkStateNotifier().RoundMbps(
        Host(), GetNetworkStateNotifier().DownlinkThroughputMbps());
  }

  return downlink_mbps_;
}

bool NetworkInformation::saveData() const {
  bool save_data =
      IsObserving() ? save_data_ : GetNetworkStateNotifier().SaveDataEnabled();

  probe::ApplyDataSaverOverride(probe::ToCoreProbeSink(GetExecutionContext()),
                                save_data);
  return save_data;
}

void NetworkInformation::ConnectionChange(
    WebConnectionType type,
    double downlink_max_mbps,
    WebEffectiveConnectionType effective_type,
    const std::optional<base::TimeDelta>& http_rtt,
    const std::optional<base::TimeDelta>& transport_rtt,
    const std::optional<double>& downlink_mbps,
    bool save_data) {
  DCHECK(GetExecutionContext()->IsContextThread());

  const String host = Host();
  uint32_t new_http_rtt_msec =
      GetNetworkStateNotifier().RoundRtt(host, http_rtt);
  double new_downlink_mbps =
      GetNetworkStateNotifier().RoundMbps(host, downlink_mbps);

  bool network_quality_estimate_changed = false;
  // Allow setting |network_quality_estimate_changed| to true only if the
  // network quality holdback experiment is not enabled.
  if (!GetNetworkStateNotifier().GetWebHoldbackEffectiveType()) {
    network_quality_estimate_changed = effective_type_ != effective_type ||
                                       http_rtt_msec_ != new_http_rtt_msec ||
                                       downlink_mbps_ != new_downlink_mbps;
  }

  // This can happen if the observer removes and then adds itself again
  // during notification, or if |transport_rtt| was the only metric that
  // changed.
  if (type_ == type && downlink_max_mbps_ == downlink_max_mbps &&
      !network_quality_estimate_changed && save_data_ == save_data) {
    return;
  }

  // If the NetInfoDownlinkMaxEnabled is not enabled, then |type| and
  // |downlink_max_mbps| should not be checked for change.
  if (!RuntimeEnabledFeatures::NetInfoDownlinkMaxEnabled() &&
      !network_quality_estimate_changed && save_data_ == save_data) {
    return;
  }

  bool type_changed =
      RuntimeEnabledFeatures::NetInfoDownlinkMaxEnabled() &&
      (type_ != type || downlink_max_mbps_ != downlink_max_mbps);

  type_ = type;
  downlink_max_mbps_ = downlink_max_mbps;
  if (network_quality_estimate_changed) {
    effective_type_ = effective_type;
    http_rtt_msec_ = new_http_rtt_msec;
    downlink_mbps_ = new_downlink_mbps;
  }
  save_data_ = save_data;

  if (type_changed)
    DispatchEvent(*Event::Create(event_type_names::kTypechange));
  DispatchEvent(*Event::Create(event_type_names::kChange));
}

const AtomicString& NetworkInformation::InterfaceName() const {
  return event_target_names::kNetworkInformation;
}

ExecutionContext* NetworkInformation::GetExecutionContext() const {
  return ExecutionContextLifecycleObserver::GetExecutionContext();
}

void NetworkInformation::AddedEventListener(
    const AtomicString& event_type,
    RegisteredEventListener& registered_listener) {
  EventTarget::AddedEventListener(event_type, registered_listener);
  MaybeShowWebHoldbackConsoleMsg();
  StartObserving();
}

void NetworkInformation::RemovedEventListener(
    const AtomicString& event_type,
    const RegisteredEventListener& registered_listener) {
  EventTarget::RemovedEventListener(event_type, registered_listener);
  if (!HasEventListeners())
    StopObserving();
}

void NetworkInformation::RemoveAllEventListeners() {
  EventTarget::RemoveAllEventListeners();
  DCHECK(!HasEventListeners());
  StopObserving();
}

bool NetworkInformation::HasPendingActivity() const {
  DCHECK(context_stopped_ || IsObserving() == HasEventListeners());

  // Prevent collection of this object when there are active listeners.
  return IsObserving();
}

void NetworkInformation::ContextDestroyed() {
  context_stopped_ = true;
  StopObserving();
}

void NetworkInformation::StartObserving() {
  if (!IsObserving() && !context_stopped_) {
    type_ = GetNetworkStateNotifier().ConnectionType();
    DCHECK(!connection_observer_handle_);
    connection_observer_handle_ =
        GetNetworkStateNotifier().AddConnectionObserver(
            this, GetExecutionContext()->GetTaskRunner(TaskType::kNetworking));
  }
}

void NetworkInformation::StopObserving() {
  if (IsObserving()) {
    DCHECK(connection_observer_handle_);
    connection_observer_handle_ = nullptr;
  }
}

const char NetworkInformation::kSupplementName[] = "NetworkInformation";

NetworkInformation* NetworkInformation::connection(NavigatorBase& navigator) {
  if (!navigator.GetExecutionContext())
    return nullptr;
  NetworkInformation* supplement =
      Supplement<NavigatorBase>::From<NetworkInformation>(navigator);
  if (!supplement) {
    supplement = MakeGarbageCollected<NetworkInformation>(navigator);
    ProvideTo(navigator, supplement);
  }
  return supplement;
}

NetworkInformation::NetworkInformation(NavigatorBase& navigator)
    : ActiveScriptWrappable<NetworkInformation>({}),
      Supplement<NavigatorBase>(navigator),
      ExecutionContextLifecycleObserver(navigator.GetExecutionContext()),
      web_holdback_console_message_shown_(false),
      context_stopped_(false) {
  std::optional<base::TimeDelta> http_rtt;
  std::optional<double> downlink_mbps;

  GetNetworkStateNotifier().GetMetricsWithWebHoldback(
      &type_, &downlink_max_mbps_, &effective_type_, &http_rtt, &downlink_mbps,
      &save_data_);

  http_rtt_msec_ = GetNetworkStateNotifier().RoundRtt(Host(), http_rtt);
  downlink_mbps_ = GetNetworkStateNotifier().RoundMbps(Host(), downlink_mbps);

  DCHECK_LE(1u, GetNetworkStateNotifier().RandomizationSalt());
  DCHECK_GE(20u, GetNetworkStateNotifier().RandomizationSalt());
}

void NetworkInformation::Trace(Visitor* visitor) const {
  EventTarget::Trace(visitor);
  Supplement<NavigatorBase>::Trace(visitor);
  ExecutionContextLifecycleObserver::Trace(visitor);
}

const String NetworkInformation::Host() const {
  return GetExecutionContext() ? GetExecutionContext()->Url().Host().ToString()
                               : String();
}

void NetworkInformation::MaybeShowWebHoldbackConsoleMsg() {
  if (web_holdback_console_message_shown_)
    return;
  web_holdback_console_message_shown_ = true;
  if (!GetNetworkStateNotifier().GetWebHoldbackEffectiveType())
    return;
  GetExecutionContext()->AddConsoleMessage(MakeGarbageCollected<ConsoleMessage>(
      mojom::ConsoleMessageSource::kOther, mojom::ConsoleMessageLevel::kWarning,
      GetConsoleLogStringForWebHoldback()));
}

}  // namespace blink
'''


NOTIFIER_SECTIONS = [
    (53, r'''constexpr size_t kNumEffectiveConnectionTypes =
    static_cast<size_t>(WebEffectiveConnectionType::kMaxValue) + 1;
'''),
    (59, r'''constexpr std::array<base::TimeDelta, kNumEffectiveConnectionTypes>
    kTypicalHttpRttEffectiveConnectionType = {
        base::Milliseconds(0),    base::Milliseconds(0),
        base::Milliseconds(3600), base::Milliseconds(1800),
        base::Milliseconds(450),  base::Milliseconds(175)};
'''),
    (68, r'''constexpr std::array<double, kNumEffectiveConnectionTypes>
    kTypicalDownlinkMbpsEffectiveConnectionType = {0,     0,     0.040,
                                                   0.075, 0.400, 1.600};
'''),
    (370, r'''double NetworkStateNotifier::GetRandomMultiplier(const String& host) const {
  // The random number should be a function of the hostname to reduce
  // cross-origin fingerprinting. The random number should also be a function
  // of randomized salt which is known only to the device. This prevents
  // origin from removing noise from the estimates.
  if (!host)
    return 1.0;

  unsigned hash = GetHash(host) + RandomizationSalt();
  double random_multiplier = 0.9 + static_cast<double>((hash % 21)) * 0.01;
  DCHECK_LE(0.90, random_multiplier);
  DCHECK_GE(1.10, random_multiplier);
  return random_multiplier;
}

uint32_t NetworkStateNotifier::RoundRtt(
    const String& host,
    const std::optional<base::TimeDelta>& rtt) const {
  if (!rtt.has_value()) {
    // RTT is unavailable. So, return the fastest value.
    return 0;
  }

  // Limit the maximum reported value and the granularity to reduce
  // fingerprinting.
  constexpr auto kMaxRtt = base::Seconds(3);
  constexpr auto kGranularity = base::Milliseconds(50);

  const base::TimeDelta modified_rtt =
      std::min(rtt.value() * GetRandomMultiplier(host), kMaxRtt);
  DCHECK_GE(modified_rtt, base::TimeDelta());
  return static_cast<uint32_t>(
      modified_rtt.RoundToMultiple(kGranularity).InMilliseconds());
}

double NetworkStateNotifier::RoundMbps(
    const String& host,
    const std::optional<double>& downlink_mbps) const {
  // Limit the size of the buckets and the maximum reported value to reduce
  // fingerprinting.
  static const size_t kBucketSize = 50;
  static const double kMaxDownlinkKbps = 10.0 * 1000;

  double downlink_kbps = 0;
  if (!downlink_mbps.has_value()) {
    // Throughput is unavailable. So, return the fastest value.
    downlink_kbps = kMaxDownlinkKbps;
  } else {
    downlink_kbps = downlink_mbps.value() * 1000;
  }
  downlink_kbps *= GetRandomMultiplier(host);

  downlink_kbps = std::min(downlink_kbps, kMaxDownlinkKbps);

  DCHECK_LE(0, downlink_kbps);
  DCHECK_GE(kMaxDownlinkKbps, downlink_kbps);
  // Round down to the nearest kBucketSize kbps value.
  double downlink_kbps_rounded =
      std::round(downlink_kbps / kBucketSize) * kBucketSize;

  // Convert from Kbps to Mbps.
  return downlink_kbps_rounded / 1000;
}

std::optional<WebEffectiveConnectionType>
NetworkStateNotifier::GetWebHoldbackEffectiveType() const {
  base::AutoLock locker(lock_);

  const NetworkState& state = has_override_ ? override_ : state_;
  // TODO (tbansal): Add a DCHECK to check that |state.on_line_initialized| is
  // true once https://crbug.com/728771 is fixed.
  return state.network_quality_web_holdback;
}

std::optional<base::TimeDelta> NetworkStateNotifier::GetWebHoldbackHttpRtt()
    const {
  std::optional<WebEffectiveConnectionType> override_ect =
      GetWebHoldbackEffectiveType();

  if (override_ect) {
    return kTypicalHttpRttEffectiveConnectionType[static_cast<size_t>(
        override_ect.value())];
  }
  return std::nullopt;
}

std::optional<double>
NetworkStateNotifier::GetWebHoldbackDownlinkThroughputMbps() const {
  std::optional<WebEffectiveConnectionType> override_ect =
      GetWebHoldbackEffectiveType();

  if (override_ect) {
    return kTypicalDownlinkMbpsEffectiveConnectionType[static_cast<size_t>(
        override_ect.value())];
  }
  return std::nullopt;
}

void NetworkStateNotifier::GetMetricsWithWebHoldback(
    WebConnectionType* type,
    double* downlink_max_mbps,
    WebEffectiveConnectionType* effective_type,
    std::optional<base::TimeDelta>* http_rtt,
    std::optional<double>* downlink_mbps,
    bool* save_data) const {
  base::AutoLock locker(lock_);
  const NetworkState& state = has_override_ ? override_ : state_;

  *type = state.type;
  *downlink_max_mbps = state.max_bandwidth_mbps;

  std::optional<WebEffectiveConnectionType> override_ect =
      state.network_quality_web_holdback;
  if (override_ect) {
    *effective_type = override_ect.value();
    *http_rtt = kTypicalHttpRttEffectiveConnectionType[static_cast<size_t>(
        override_ect.value())];
    *downlink_mbps =
        kTypicalDownlinkMbpsEffectiveConnectionType[static_cast<size_t>(
            override_ect.value())];
  } else {
    *effective_type = state.effective_type;
    *http_rtt = state.http_rtt;
    *downlink_mbps = state.downlink_throughput_mbps;
  }
  *save_data = state.save_data;
}
'''),
]


# Plumbing only: no NetworkInformation method bodies or notifier rounding here.
CPP_SUPPORT = r'''
#include <algorithm>
#include <array>
#include <cassert>
#include <cmath>
#include <compare>
#include <cstdint>
#include <cstdlib>
#include <functional>
#include <iostream>
#include <limits>
#include <memory>
#include <optional>
#include <string>
#include <utility>
#include <vector>
#define DCHECK(x) assert(x)
#define DCHECK_LE(a, b) assert((a) <= (b))
#define DCHECK_GE(a, b) assert((a) >= (b))
#define NOTREACHED() std::abort()
namespace base {
// Finite microsecond arithmetic suffices for these nonnegative test inputs.
struct TimeDelta {
  int64_t us = 0;
  auto operator<=>(const TimeDelta&) const = default;
  TimeDelta operator*(double multiplier) const {
    return {static_cast<int64_t>(us * multiplier)};
  }
  TimeDelta RoundToMultiple(TimeDelta interval) const {
    return {static_cast<int64_t>(std::round(double(us) / interval.us)) * interval.us};
  }
  int64_t InMilliseconds() const { return us / 1000; }
};
constexpr TimeDelta Milliseconds(int64_t value) { return {value * 1000}; }
constexpr TimeDelta Seconds(int64_t value) { return {value * 1000000}; }
struct Lock {};
struct AutoLock { explicit AutoLock(Lock&) {} };
}
struct String {
  std::string text;
  unsigned hash = 0;
  String() = default;
  String(const char* value) : text(value) {}
  String(std::string value, unsigned hash_value = 0) : text(std::move(value)), hash(hash_value) {}
  explicit operator bool() const { return !text.empty(); }
  String ToString() const { return *this; }
};
unsigned GetHash(const String& host) { return host.hash; }
using AtomicString = std::string;
enum WebConnectionType {
  kWebConnectionTypeCellular2G, kWebConnectionTypeCellular3G,
  kWebConnectionTypeCellular4G, kWebConnectionTypeBluetooth,
  kWebConnectionTypeEthernet, kWebConnectionTypeWifi, kWebConnectionTypeWimax,
  kWebConnectionTypeOther, kWebConnectionTypeNone, kWebConnectionTypeUnknown
};
enum class WebEffectiveConnectionType {
  kTypeUnknown, kTypeOffline, kTypeSlow2G, kType2G, kType3G, kType4G,
  kMaxValue = kType4G
};
struct V8ConnectionType {
  enum class Enum { kCellular, kBluetooth, kEthernet, kWifi, kWimax, kOther, kNone, kUnknown };
  Enum value;
  explicit V8ConnectionType(Enum input) : value(input) {}
};
struct V8EffectiveConnectionType {
  enum class Enum { kSlow2G, k2G, k3G, k4G };
  Enum value;
  explicit V8EffectiveConnectionType(Enum input) : value(input) {}
};
struct RuntimeEnabledFeatures {
  static inline bool constant_type = false;
  static inline bool downlink_max = true;
  static bool NetInfoConstantTypeEnabled() { return constant_type; }
  static bool NetInfoDownlinkMaxEnabled() { return downlink_max; }
};
namespace mojom {
enum class ConsoleMessageSource { kOther };
enum class ConsoleMessageLevel { kWarning };
}
struct ConsoleMessage {
  String text;
  ConsoleMessage(mojom::ConsoleMessageSource, mojom::ConsoleMessageLevel, String value)
      : text(std::move(value)) {}
};
template<class T, class... Args> T* MakeGarbageCollected(Args&&... args) {
  return new T(std::forward<Args>(args)...);
}
enum class TaskType { kNetworking };
struct URL {
  String host;
  String Host() const { return host; }
};
struct ExecutionContext {
  URL url;
  bool context_thread = true;
  std::optional<bool> data_saver_override;
  unsigned probe_calls = 0;
  std::vector<std::string> messages;
  bool IsContextThread() const { return context_thread; }
  const URL& Url() const { return url; }
  int GetTaskRunner(TaskType type) const { assert(type == TaskType::kNetworking); return 17; }
  void AddConsoleMessage(ConsoleMessage* raw) {
    std::unique_ptr<ConsoleMessage> message(raw);
    messages.push_back(message->text.text);
  }
};
namespace probe {
ExecutionContext* ToCoreProbeSink(ExecutionContext* context) { return context; }
void ApplyDataSaverOverride(ExecutionContext* context, bool& value) {
  if (!context) return;
  ++context->probe_calls;
  if (context->data_saver_override) value = *context->data_saver_override;
}
}
namespace event_type_names {
const AtomicString kTypechange = "typechange", kChange = "change";
}
namespace event_target_names {
const AtomicString kNetworkInformation = "NetworkInformation";
}
struct Event {
  AtomicString type;
  static std::unique_ptr<Event> Create(const AtomicString& type) {
    return std::make_unique<Event>(Event{type});
  }
};
struct RegisteredEventListener {};
struct EventTarget {
  int listeners = 0;
  std::vector<std::string> events;
  std::function<void(const Event&)> on_dispatch;
  void AddedEventListener(const AtomicString&, RegisteredEventListener&) { ++listeners; }
  void RemovedEventListener(const AtomicString&, const RegisteredEventListener&) {
    assert(listeners > 0); --listeners;
  }
  void RemoveAllEventListeners() { listeners = 0; }
  bool HasEventListeners() const { return listeners != 0; }
  void DispatchEvent(const Event& event) {
    events.push_back(event.type);
    if (on_dispatch) on_dispatch(event);
  }
};
struct NavigatorBase {
  ExecutionContext* context;
  ExecutionContext* GetExecutionContext() const { return context; }
};
template<class T> struct ActiveScriptWrappable { explicit ActiveScriptWrappable(int) {} };
template<class T> struct Supplement { explicit Supplement(T&) {} };
struct ExecutionContextLifecycleObserver {
  ExecutionContext* context;
  explicit ExecutionContextLifecycleObserver(ExecutionContext* value) : context(value) {}
  ExecutionContext* GetExecutionContext() const { return context; }
};
struct NetworkInformation;
struct NetworkStateNotifier {
  struct NetworkState {
    WebConnectionType type = kWebConnectionTypeWifi;
    double max_bandwidth_mbps = 100;
    WebEffectiveConnectionType effective_type = WebEffectiveConnectionType::kType4G;
    std::optional<base::TimeDelta> http_rtt = base::Milliseconds(100);
    std::optional<base::TimeDelta> transport_rtt = base::Milliseconds(50);
    std::optional<double> downlink_throughput_mbps = 2.0;
    bool save_data = false;
    std::optional<WebEffectiveConnectionType> network_quality_web_holdback;
  };
  struct NetworkStateObserverHandle {
    NetworkStateNotifier* owner;
    NetworkInformation* observer;
    ~NetworkStateObserverHandle() {
      assert(std::erase(owner->observers, observer) == 1);
      ++owner->removed;
    }
  };
  NetworkState state_, override_;
  bool has_override_ = false;
  mutable base::Lock lock_;
  uint8_t salt = 10;
  unsigned added = 0, removed = 0;
  std::vector<NetworkInformation*> observers;
  const NetworkState& ActiveState() const { return has_override_ ? override_ : state_; }
  WebConnectionType ConnectionType() const { return ActiveState().type; }
  double MaxBandwidth() const { return ActiveState().max_bandwidth_mbps; }
  WebEffectiveConnectionType EffectiveType() const { return ActiveState().effective_type; }
  std::optional<base::TimeDelta> HttpRtt() const { return ActiveState().http_rtt; }
  std::optional<double> DownlinkThroughputMbps() const { return ActiveState().downlink_throughput_mbps; }
  bool SaveDataEnabled() const { return ActiveState().save_data; }
  uint8_t RandomizationSalt() const { return salt; }
  double GetRandomMultiplier(const String&) const;
  uint32_t RoundRtt(const String&, const std::optional<base::TimeDelta>&) const;
  double RoundMbps(const String&, const std::optional<double>&) const;
  std::optional<WebEffectiveConnectionType> GetWebHoldbackEffectiveType() const;
  std::optional<base::TimeDelta> GetWebHoldbackHttpRtt() const;
  std::optional<double> GetWebHoldbackDownlinkThroughputMbps() const;
  void GetMetricsWithWebHoldback(WebConnectionType*, double*, WebEffectiveConnectionType*,
                                std::optional<base::TimeDelta>*, std::optional<double>*, bool*) const;
  std::unique_ptr<NetworkStateObserverHandle> AddConnectionObserver(NetworkInformation* observer, int task) {
    assert(task == 17);
    assert(std::find(observers.begin(), observers.end(), observer) == observers.end());
    auto handle = std::make_unique<NetworkStateObserverHandle>();
    handle->owner = this;
    handle->observer = observer;
    observers.push_back(observer);
    ++added;
    return handle;
  }
};
NetworkStateNotifier& GetNetworkStateNotifier() {
  static NetworkStateNotifier notifier;
  return notifier;
}
struct NetworkInformation : EventTarget, ActiveScriptWrappable<NetworkInformation>,
                            Supplement<NavigatorBase>, ExecutionContextLifecycleObserver {
  WebConnectionType type_;
  double downlink_max_mbps_;
  WebEffectiveConnectionType effective_type_;
  uint32_t http_rtt_msec_;
  double downlink_mbps_;
  bool save_data_, web_holdback_console_message_shown_, context_stopped_;
  std::unique_ptr<NetworkStateNotifier::NetworkStateObserverHandle> connection_observer_handle_;
  explicit NetworkInformation(NavigatorBase&);
  ~NetworkInformation();
  bool IsObserving() const;
  V8ConnectionType type() const;
  double downlinkMax() const;
  V8EffectiveConnectionType effectiveType();
  uint32_t rtt();
  double downlink();
  bool saveData() const;
  void ConnectionChange(WebConnectionType, double, WebEffectiveConnectionType,
                        const std::optional<base::TimeDelta>&, const std::optional<base::TimeDelta>&,
                        const std::optional<double>&, bool);
  const AtomicString& InterfaceName() const;
  ExecutionContext* GetExecutionContext() const;
  void AddedEventListener(const AtomicString&, RegisteredEventListener&);
  void RemovedEventListener(const AtomicString&, const RegisteredEventListener&);
  void RemoveAllEventListeners();
  bool HasPendingActivity() const;
  void ContextDestroyed();
  void StartObserving();
  void StopObserving();
  const String Host() const;
  void MaybeShowWebHoldbackConsoleMsg();
};
'''


CPP_TESTS = r'''
using ECT = WebEffectiveConnectionType;
using Effective = V8EffectiveConnectionType::Enum;
using Type = V8ConnectionType::Enum;
using State = NetworkStateNotifier::NetworkState;
struct Values {
  Type type;
  double max;
  Effective effective;
  uint32_t rtt;
  double downlink;
  bool save;
  bool operator==(const Values&) const = default;
};
Values Read(NetworkInformation& info) {
  return {info.type().value, info.downlinkMax(), info.effectiveType().value,
          info.rtt(), info.downlink(), info.saveData()};
}
void Expect(NetworkInformation& info, Values expected) {
  assert(Read(info) == expected);
  assert(Read(info) == expected);
}
void Deliver(NetworkInformation& info, const State& state) {
  const auto& observers = GetNetworkStateNotifier().observers;
  assert(std::find(observers.begin(), observers.end(), &info) != observers.end());
  info.ConnectionChange(state.type, state.max_bandwidth_mbps, state.effective_type,
                        state.http_rtt, state.transport_rtt, state.downlink_throughput_mbps,
                        state.save_data);
}
void Listen(NetworkInformation& info) {
  RegisteredEventListener listener;
  info.AddedEventListener(event_type_names::kChange, listener);
}
void ExpectEvents(NetworkInformation& info, std::vector<std::string> expected) {
  assert(info.events == expected);
  info.events.clear();
}
struct Fixture {
  ExecutionContext context;
  NavigatorBase navigator{&context};
  NetworkInformation info{navigator};
  ~Fixture() { info.RemoveAllEventListeners(); }
};
const Values kInitial{Type::kWifi, 100, Effective::k4G, 100, 2.0, false};

int main(int argc, char** argv) {
  assert(argc == 2);
  const std::string test = argv[1];
  auto& notifier = GetNetworkStateNotifier();
  auto& state = notifier.state_;
  if (test == "native") {
    Fixture f;
    Expect(f.info, kInitial);
    assert(!f.info.IsObserving() && !f.info.HasPendingActivity());
    assert(f.info.InterfaceName() == "NetworkInformation");
    state.type = kWebConnectionTypeEthernet;
    state.max_bandwidth_mbps = 1000;
    state.effective_type = ECT::kType3G;
    state.http_rtt = base::Milliseconds(449);
    state.downlink_throughput_mbps = 0.426;
    state.save_data = true;
    Expect(f.info, {Type::kEthernet, 1000, Effective::k3G, 450, 0.45, true});
    assert(f.info.http_rtt_msec_ == 100 && f.info.downlink_mbps_ == 2);
    assert(f.info.effective_type_ == ECT::kType4G && !f.info.save_data_);
    state.type = kWebConnectionTypeNone;
    state.max_bandwidth_mbps = 0;
    state.effective_type = ECT::kTypeOffline;
    state.http_rtt.reset();
    state.downlink_throughput_mbps.reset();
    Expect(f.info, {Type::kNone, 0, Effective::k4G, 0, 10, true});
    state.http_rtt = base::Milliseconds(0);
    state.downlink_throughput_mbps = 0;
    assert(f.info.rtt() == 0 && f.info.downlink() == 0);
    assert(f.info.events.empty() && notifier.added == 0 && f.context.messages.empty());
  } else if (test == "enum-mapping") {
    Fixture f;
    const std::array types = {
      std::pair{kWebConnectionTypeCellular2G, Type::kCellular},
      std::pair{kWebConnectionTypeCellular3G, Type::kCellular},
      std::pair{kWebConnectionTypeCellular4G, Type::kCellular},
      std::pair{kWebConnectionTypeBluetooth, Type::kBluetooth},
      std::pair{kWebConnectionTypeEthernet, Type::kEthernet},
      std::pair{kWebConnectionTypeWifi, Type::kWifi},
      std::pair{kWebConnectionTypeWimax, Type::kWimax},
      std::pair{kWebConnectionTypeOther, Type::kOther},
      std::pair{kWebConnectionTypeNone, Type::kNone},
      std::pair{kWebConnectionTypeUnknown, Type::kUnknown}};
    const std::array effective = {
      std::pair{ECT::kTypeUnknown, Effective::k4G},
      std::pair{ECT::kTypeOffline, Effective::k4G},
      std::pair{ECT::kTypeSlow2G, Effective::kSlow2G},
      std::pair{ECT::kType2G, Effective::k2G},
      std::pair{ECT::kType3G, Effective::k3G},
      std::pair{ECT::kType4G, Effective::k4G}};
    for (bool observing : {false, true}) {
      if (observing) Listen(f.info);
      for (auto [native, expected] : types) {
        state.type = native;
        if (observing) Deliver(f.info, state);
        assert(f.info.type().value == expected);
      }
      for (auto [native, expected] : effective) {
        state.effective_type = native;
        if (observing) Deliver(f.info, state);
        assert(f.info.effectiveType().value == expected);
      }
    }
  } else if (test == "observing") {
    Fixture f;
    Listen(f.info);
    assert(f.info.IsObserving() && f.info.HasPendingActivity());
    State next = state;
    next.type = kWebConnectionTypeEthernet;
    next.max_bandwidth_mbps = 1000;
    next.effective_type = ECT::kType3G;
    next.http_rtt = base::Milliseconds(451);
    next.downlink_throughput_mbps = 0.401;
    next.save_data = true;
    state = next;
    Expect(f.info, kInitial);
    const Values expected{Type::kEthernet, 1000, Effective::k3G, 450, 0.4, true};
    f.info.on_dispatch = [&](const Event&) { Expect(f.info, expected); };
    Deliver(f.info, next);
    ExpectEvents(f.info, {"typechange", "change"});
    Expect(f.info, expected);
    state = State{};
    Expect(f.info, expected);
    f.info.RemoveAllEventListeners();
    Expect(f.info, kInitial);
    assert(notifier.added == 1 && notifier.removed == 1);
  } else if (test == "holdback") {
    struct Row { ECT type; Effective exposed; uint32_t rtt; double downlink; };
    const std::array rows = {
      Row{ECT::kTypeUnknown, Effective::k4G, 0, 0},
      Row{ECT::kTypeOffline, Effective::k4G, 0, 0},
      Row{ECT::kTypeSlow2G, Effective::kSlow2G, 3000, 0.05},
      Row{ECT::kType2G, Effective::k2G, 1800, 0.1},
      Row{ECT::kType3G, Effective::k3G, 450, 0.4},
      Row{ECT::kType4G, Effective::k4G, 200, 1.6}};
    for (bool observing : {false, true}) {
      for (auto row : rows) {
        state = State{};
        state.network_quality_web_holdback = row.type;
        Fixture f;
        assert(f.info.effective_type_ == row.type);
        assert(f.info.http_rtt_msec_ == row.rtt && f.info.downlink_mbps_ == row.downlink);
        if (observing) Listen(f.info);
        Values expected{Type::kWifi, 100, row.exposed, row.rtt, row.downlink, false};
        Expect(f.info, expected);
        assert(f.context.messages.size() == 1);
        assert(f.context.messages[0] ==
               "Network quality values are overridden using a holdback experiment, and so may be inaccurate");
        state.effective_type = ECT::kTypeSlow2G;
        state.http_rtt = base::Milliseconds(2400);
        state.downlink_throughput_mbps = 8;
        if (observing) Deliver(f.info, state);
        Expect(f.info, expected);
        ExpectEvents(f.info, {});
        state.save_data = true;
        if (observing) { Deliver(f.info, state); ExpectEvents(f.info, {"change"}); }
        expected.save = true;
        Expect(f.info, expected);
        state.type = kWebConnectionTypeBluetooth;
        state.max_bandwidth_mbps = 3;
        if (observing) { Deliver(f.info, state); ExpectEvents(f.info, {"typechange", "change"}); }
        expected.type = Type::kBluetooth;
        expected.max = 3;
        Expect(f.info, expected);
        assert(f.info.http_rtt_msec_ == row.rtt && f.info.downlink_mbps_ == row.downlink);
        assert(f.context.messages.size() == 1);
      }
    }
  } else if (test == "holdback-precedence") {
    for (bool observing : {false, true}) {
      state = State{};
      Fixture f;
      if (observing) Listen(f.info);
      Expect(f.info, kInitial);
      state.network_quality_web_holdback = ECT::kType3G;
      state.effective_type = ECT::kType2G;
      state.http_rtt = base::Milliseconds(1800);
      state.downlink_throughput_mbps = 0.075;
      if (observing) Deliver(f.info, state);
      Expect(f.info, {Type::kWifi, 100, Effective::k3G, 450, 0.4, false});
      assert(f.info.effective_type_ == ECT::kType4G && f.info.http_rtt_msec_ == 100);
      assert(f.info.downlink_mbps_ == 2 && f.context.messages.empty());
      ExpectEvents(f.info, {});
      state.network_quality_web_holdback = ECT::kTypeOffline;
      Expect(f.info, {Type::kWifi, 100, Effective::k4G, 0, 0, false});
      state.network_quality_web_holdback.reset();
      if (observing) {
        Expect(f.info, kInitial);
        Deliver(f.info, state);
        ExpectEvents(f.info, {"change"});
      }
      Expect(f.info, {Type::kWifi, 100, Effective::k2G, 1800, 0.1, false});
    }
  } else if (test == "notifier-override") {
    notifier.override_ = state;
    auto& override = notifier.override_;
    override.type = kWebConnectionTypeEthernet;
    override.max_bandwidth_mbps = 1000;
    override.effective_type = ECT::kType2G;
    override.http_rtt = base::Milliseconds(1800);
    override.downlink_throughput_mbps = 0.075;
    override.save_data = true;
    notifier.has_override_ = true;
    Fixture f;
    const Values expected{Type::kEthernet, 1000, Effective::k2G, 1800, 0.1, true};
    Expect(f.info, expected);
    Listen(f.info);
    state.effective_type = ECT::kTypeSlow2G;
    state.http_rtt = base::Milliseconds(2600);
    Expect(f.info, expected);
    override.network_quality_web_holdback = ECT::kType3G;
    Deliver(f.info, override);
    ExpectEvents(f.info, {});
    Expect(f.info, {Type::kEthernet, 1000, Effective::k3G, 450, 0.4, true});
    assert(f.info.http_rtt_msec_ == 1800 && f.info.downlink_mbps_ == 0.1);
    notifier.has_override_ = false;
    Expect(f.info, expected);
    Deliver(f.info, state);
    ExpectEvents(f.info, {"typechange", "change"});
    Expect(f.info, {Type::kWifi, 100, Effective::kSlow2G, 2600, 2, false});
  } else if (test == "rounding") {
    struct Row { std::optional<int64_t> input; uint32_t expected; };
    for (auto row : {Row{std::nullopt, 0}, Row{0, 0}, Row{24, 0}, Row{25, 50},
                     Row{74, 50}, Row{75, 100}, Row{2974, 2950}, Row{2975, 3000},
                     Row{3000, 3000}, Row{9000, 3000}}) {
      state.http_rtt = row.input ? std::optional(base::Milliseconds(*row.input)) : std::nullopt;
      Fixture f;
      assert(f.info.rtt() == row.expected);
      Listen(f.info);
      assert(f.info.rtt() == row.expected);
    }
    struct Rate { std::optional<double> input; double expected; };
    for (auto row : {Rate{std::nullopt, 10}, Rate{0, 0}, Rate{0.024, 0}, Rate{0.025, 0.05},
                     Rate{0.074, 0.05}, Rate{0.075, 0.1}, Rate{9.974, 9.95},
                     Rate{9.975, 10}, Rate{10, 10}, Rate{100, 10}}) {
      state.downlink_throughput_mbps = row.input;
      Fixture f;
      assert(f.info.downlink() == row.expected);
      Listen(f.info);
      assert(f.info.downlink() == row.expected);
    }
  } else if (test == "save-data") {
    Fixture f;
    assert(!f.info.saveData());
    state.save_data = true;
    assert(f.info.saveData());
    for (bool value : {false, true}) {
      f.context.data_saver_override = value;
      assert(f.info.saveData() == value);
      assert(state.save_data && !f.info.save_data_);
    }
    f.context.data_saver_override.reset();
    state.save_data = false;
    Listen(f.info);
    state.save_data = true;
    assert(!f.info.saveData());
    f.context.data_saver_override = false;
    f.info.on_dispatch = [&](const Event&) { assert(!f.info.saveData() && f.info.save_data_); };
    Deliver(f.info, state);
    ExpectEvents(f.info, {"change"});
    assert(!f.info.saveData() && f.info.save_data_ && state.save_data);
    f.context.data_saver_override = true;
    assert(f.info.saveData());
    Deliver(f.info, state);
    ExpectEvents(f.info, {});
    f.context.data_saver_override.reset();
    assert(f.info.saveData());
    state.save_data = false;
    assert(f.info.saveData());
    f.info.RemoveAllEventListeners();
    assert(!f.info.saveData() && f.context.probe_calls >= 10);
  } else if (test == "change-suppression") {
    Fixture f;
    Listen(f.info);
    Deliver(f.info, state);
    state.transport_rtt = base::Milliseconds(999);
    Deliver(f.info, state);
    state.http_rtt = base::Milliseconds(124);
    state.downlink_throughput_mbps = 2.024;
    Deliver(f.info, state);
    ExpectEvents(f.info, {});
    Expect(f.info, kInitial);
    state.http_rtt = base::Milliseconds(125);
    Deliver(f.info, state);
    ExpectEvents(f.info, {"change"});
    assert(f.info.rtt() == 150);
    state.downlink_throughput_mbps = 2.026;
    Deliver(f.info, state);
    ExpectEvents(f.info, {"change"});
    assert(f.info.downlink() == 2.05);
    state.effective_type = ECT::kType3G;
    Deliver(f.info, state);
    ExpectEvents(f.info, {"change"});
    assert(f.info.effectiveType().value == Effective::k3G);
    Deliver(f.info, state);
    ExpectEvents(f.info, {});
    state.http_rtt.reset();
    state.downlink_throughput_mbps.reset();
    Deliver(f.info, state);
    ExpectEvents(f.info, {"change"});
    assert(f.info.rtt() == 0 && f.info.downlink() == 10);
    Deliver(f.info, state);
    ExpectEvents(f.info, {});
  } else if (test == "feature-gates") {
    for (bool constant : {false, true}) {
      for (bool enabled : {false, true}) {
        state = State{};
        RuntimeEnabledFeatures::constant_type = constant;
        RuntimeEnabledFeatures::downlink_max = enabled;
        Fixture f;
        const double maximum = constant ? std::numeric_limits<double>::infinity() : 100;
        Expect(f.info, {constant ? Type::kUnknown : Type::kWifi, maximum,
                        Effective::k4G, 100, 2, false});
        Listen(f.info);
        state.type = kWebConnectionTypeEthernet;
        state.max_bandwidth_mbps = 1000;
        Deliver(f.info, state);
        ExpectEvents(f.info, enabled ? std::vector<std::string>{"typechange", "change"}
                                    : std::vector<std::string>{});
        assert(f.info.type_ == (enabled ? kWebConnectionTypeEthernet : kWebConnectionTypeWifi));
        assert(f.info.downlink_max_mbps_ == (enabled ? 1000 : 100));
        if (constant) assert(f.info.type().value == Type::kUnknown && std::isinf(f.info.downlinkMax()));
        state.http_rtt = base::Milliseconds(400);
        Deliver(f.info, state);
        ExpectEvents(f.info, {"change"});
        assert(f.info.type_ == kWebConnectionTypeEthernet && f.info.downlink_max_mbps_ == 1000);
        assert(f.info.rtt() == 400);
        state.max_bandwidth_mbps = 500;
        Deliver(f.info, state);
        ExpectEvents(f.info, enabled ? std::vector<std::string>{"typechange", "change"}
                                    : std::vector<std::string>{});
        state.save_data = true;
        Deliver(f.info, state);
        ExpectEvents(f.info, {"change"});
        assert(f.info.saveData() && f.info.downlink_max_mbps_ == 500);
      }
    }
  } else if (test == "event-order") {
    Fixture f;
    Listen(f.info);
    State next = state;
    next.type = kWebConnectionTypeCellular3G;
    next.max_bandwidth_mbps = 42;
    next.effective_type = ECT::kType2G;
    next.http_rtt = base::Milliseconds(1800);
    next.downlink_throughput_mbps = 0.075;
    next.save_data = true;
    const Values expected{Type::kCellular, 42, Effective::k2G, 1800, 0.1, true};
    int callbacks = 0;
    f.info.on_dispatch = [&](const Event& event) {
      Expect(f.info, expected);
      assert(event.type == (callbacks++ == 0 ? "typechange" : "change"));
      if (event.type == "typechange") Deliver(f.info, next);
    };
    Deliver(f.info, next);
    assert(callbacks == 2);
    ExpectEvents(f.info, {"typechange", "change"});
    Expect(f.info, expected);
    assert(state.type == kWebConnectionTypeWifi && state.http_rtt == base::Milliseconds(100));
  } else if (test == "lifecycle") {
    Fixture f;
    RegisteredEventListener listener;
    Listen(f.info);
    Listen(f.info);
    assert(notifier.added == 1 && f.info.HasPendingActivity());
    f.info.RemovedEventListener(event_type_names::kChange, listener);
    assert(f.info.IsObserving() && notifier.removed == 0);
    f.info.RemovedEventListener(event_type_names::kChange, listener);
    assert(!f.info.IsObserving() && !f.info.HasPendingActivity() && notifier.removed == 1);
    state.type = kWebConnectionTypeEthernet;
    state.http_rtt = base::Milliseconds(500);
    state.downlink_throughput_mbps = 5;
    Listen(f.info);
    assert(f.info.type().value == Type::kEthernet && f.info.rtt() == 100 && f.info.downlink() == 2);
    Deliver(f.info, state);
    ExpectEvents(f.info, {"change"});
    assert(f.info.rtt() == 500 && f.info.downlink() == 5);
    f.info.RemoveAllEventListeners();
    Listen(f.info);
    Deliver(f.info, state);
    ExpectEvents(f.info, {});
    assert(notifier.added == 3 && notifier.removed == 2);
    f.info.ContextDestroyed();
    assert(!f.info.IsObserving() && !f.info.HasPendingActivity());
    assert(notifier.removed == 3);
    Listen(f.info);
    assert(notifier.added == 3 && !f.info.IsObserving());
    f.info.RemoveAllEventListeners();
    NavigatorBase detached{nullptr};
    NetworkInformation without_context(detached);
    assert(!without_context.Host());
  } else if (test == "host-rounding") {
    struct Row { unsigned hash; uint8_t salt; double factor; uint32_t rtt; double rate; double missing; };
    for (auto row : {Row{20, 1, 0.9, 900, 4.5, 9}, Row{9, 1, 1.0, 1000, 5, 10},
                     Row{19, 1, 1.1, 1100, 5.5, 10}, Row{0, 20, 1.1, 1100, 5.5, 10}}) {
      notifier.salt = row.salt;
      state = State{};
      state.http_rtt = base::Milliseconds(1000);
      state.downlink_throughput_mbps = 5;
      ExecutionContext context;
      context.url.host = String("fixture.test", row.hash);
      NavigatorBase navigator{&context};
      NetworkInformation info(navigator);
      assert(std::abs(notifier.GetRandomMultiplier(info.Host()) - row.factor) < 1e-12);
      assert(info.rtt() == row.rtt && info.downlink() == row.rate);
      Listen(info);
      assert(info.rtt() == row.rtt && info.downlink() == row.rate);
      Deliver(info, state);
      ExpectEvents(info, {});
      state.http_rtt = base::Milliseconds(9000);
      state.downlink_throughput_mbps = 50;
      Deliver(info, state);
      ExpectEvents(info, {"change"});
      assert(info.rtt() == 3000 && info.downlink() == 10);
      state.http_rtt.reset();
      state.downlink_throughput_mbps.reset();
      Deliver(info, state);
      ExpectEvents(info, {"change"});
      assert(info.rtt() == 0 && info.downlink() == row.missing);
      info.RemoveAllEventListeners();
      assert(info.rtt() == 0 && info.downlink() == row.missing);
    }
  } else {
    assert(false);
  }
  assert(notifier.observers.empty());
  std::cout << test << ": PASS\n";
}
'''
