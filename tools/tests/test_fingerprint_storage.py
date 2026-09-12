"""Storage estimate retirement regressions with independent Chromium 152 fixtures.

Excerpts were copied from .chromix-build-verify/src-first-attempt (152.0.7977.82)
and matched src-strict-patch-attempt and sparse-real110-81cwzua4/raw byte-for-byte.
CHROMIX_STORAGE_BASELINE_ROOT optionally verifies full-file hashes and patch
round trips on copies of a local pre-Chromix tree. Fixtures never use diff text.
The C++ stubs execute native/patched callbacks, not Mojo, V8 or disk enforcement.
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
PATCH_BIN = shutil.which("gpatch") or shutil.which("patch")
CXX = shutil.which("clang++") or shutil.which("g++")
TARGETS = {
    "0034": "third_party/blink/renderer/modules/quota/storage_manager.cc",
    "0039": "third_party/blink/renderer/modules/buckets/storage_bucket.cc",
}
PATCHES = {
    "0034": ROOT / "patches/0034-third_party-blink-renderer-modules-quota-storage_manager-cc.patch",
    "0039": ROOT / "patches/0039-third_party-blink-renderer-modules-buckets-storage_bucket-cc.patch",
}
SOURCE_SHA256 = {
    "0034": "5289cebb75c1eeb36d5c284c5ab7ed6f861e6fdc90a12c5f9513a5bb60aa371a",
    "0039": "28d9b4858591500829179b4dfbf22b1549107b2dbb355ceac536b35c5d825618",
}
HUNK_STARTS = {"0034": 68, "0039": 266}

# Line numbers and complete callback bodies are pinned to the independent source.
SOURCE_SECTIONS = {
    "0034": [(40, '''const char kGenericErrorMessage[] =
    "Internal error when calculating storage usage.";
const char kAbortErrorMessage[] = "The operation was aborted due to shutdown.";

void QueryStorageUsageAndQuotaCallback(
    ScriptPromiseResolver<StorageEstimate>* resolver,
    mojom::blink::QuotaStatusCode status_code,
    int64_t usage_in_bytes,
    int64_t quota_in_bytes,
    UsageBreakdownPtr usage_breakdown) {
  const char* error_message = nullptr;
  switch (status_code) {
    case mojom::blink::QuotaStatusCode::kOk:
      break;
    case mojom::blink::QuotaStatusCode::kErrorNotSupported:
    case mojom::blink::QuotaStatusCode::kErrorInvalidModification:
    case mojom::blink::QuotaStatusCode::kErrorInvalidAccess:
      NOTREACHED();
    case mojom::blink::QuotaStatusCode::kUnknown:
      error_message = kGenericErrorMessage;
      break;
    case mojom::blink::QuotaStatusCode::kErrorAbort:
      error_message = kAbortErrorMessage;
      break;
  }
  if (error_message) {
    resolver->Reject(V8ThrowException::CreateTypeError(
        resolver->GetScriptState()->GetIsolate(), error_message));
    return;
  }

  StorageEstimate* estimate = StorageEstimate::Create();
  estimate->setUsage(usage_in_bytes);
  estimate->setQuota(quota_in_bytes);

  // We only want to show usage details for systems that are used by the app,
  // this way we do not create any web compatibility issues by unecessarily
  // exposing obsoleted/proprietary storage systems, but also report when
  // those systems are in use.
  StorageUsageDetails* details = StorageUsageDetails::Create();
  if (usage_breakdown->indexedDatabase) {
    details->setIndexedDB(usage_breakdown->indexedDatabase);
  }
  if (usage_breakdown->serviceWorkerCache) {
    details->setCaches(usage_breakdown->serviceWorkerCache);
  }
  if (usage_breakdown->serviceWorker) {
    details->setServiceWorkerRegistrations(usage_breakdown->serviceWorker);
  }
  if (usage_breakdown->fileSystem) {
    details->setFileSystem(usage_breakdown->fileSystem);
  }

  estimate->setUsageDetails(details);

  resolver->Resolve(estimate);
}
'''), (151, '''ScriptPromise<StorageEstimate> StorageManager::estimate(
    ScriptState* script_state,
    ExceptionState& exception_state) {
  ExecutionContext* execution_context = ExecutionContext::From(script_state);
  DCHECK(execution_context->IsSecureContext());  // [SecureContext] in IDL

  // The BlinkIDL definition for estimate() already has a [MeasureAs] attribute,
  // so the kQuotaRead use counter must be explicitly updated.
  UseCounter::Count(execution_context, WebFeature::kQuotaRead);

  const SecurityOrigin* security_origin =
      execution_context->GetSecurityOrigin();
  if (security_origin->IsOpaque()) {
    exception_state.ThrowTypeError(kUniqueOriginErrorMessage);
    return EmptyPromise();
  }

  auto* resolver = MakeGarbageCollected<ScriptPromiseResolver<StorageEstimate>>(
      script_state, exception_state.GetContext());
  auto promise = resolver->Promise();

  auto callback = resolver->WrapCallbackInScriptScope(
      BindOnce(&QueryStorageUsageAndQuotaCallback));
  GetQuotaHost(execution_context)
      ->QueryStorageUsageAndQuota(mojo::WrapCallbackWithDefaultInvokeIfNotRun(
          std::move(callback), mojom::blink::QuotaStatusCode::kErrorAbort, 0, 0,
          nullptr));
  return promise;
}
''')],
    "0039": [(79, '''ScriptPromise<StorageEstimate> StorageBucket::estimate(
    ScriptState* script_state) {
  auto* resolver = MakeGarbageCollected<ScriptPromiseResolver<StorageEstimate>>(
      script_state);
  auto promise = resolver->Promise();

  // The context may be destroyed and the mojo connection unbound. However the
  // object may live on, reject any requests after the context is destroyed.
  if (!remote_.is_bound()) {
    resolver->Reject(MakeGarbageCollected<DOMException>(
        DOMExceptionCode::kInvalidStateError));
    return promise;
  }

  remote_->Estimate(BindOnce(&StorageBucket::DidGetEstimate,
                             WrapPersistent(this), WrapPersistent(resolver)));
  return promise;
}
'''), (257, '''void StorageBucket::DidGetEstimate(
    ScriptPromiseResolver<StorageEstimate>* resolver,
    int64_t current_usage,
    int64_t current_quota,
    bool success) {
  if (!success) {
    resolver->Reject(MakeGarbageCollected<DOMException>(
        DOMExceptionCode::kUnknownError,
        "Unknown error occurred while getting estimate."));
    return;
  }

  StorageEstimate* estimate = StorageEstimate::Create();
  estimate->setUsage(current_usage);
  estimate->setQuota(current_quota);
  StorageUsageDetails* details = StorageUsageDetails::Create();
  estimate->setUsageDetails(details);
  resolver->Resolve(estimate);
}
''')],
}


def native_fixture(number):
    lines = []
    for first, text in SOURCE_SECTIONS[number]:
        assert len(lines) < first
        lines.extend("// unrelated native source\n" for _ in range(first - 1 - len(lines)))
        lines.extend(text.splitlines(keepends=True))
    lines.append("// trailing native source\n")
    return "".join(lines).encode()


def write_fixture(directory, number, data=None):
    target = directory / TARGETS[number]
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(native_fixture(number) if data is None else data)
    return target


def apply_patch(directory, number, *, reverse=False, dry_run=False):
    if not PATCH_BIN:
        pytest.skip("GNU patch is required")
    command = [PATCH_BIN, "-p1", "--fuzz=0", "--batch", "--binary", "--get=0",
               "--no-backup-if-mismatch", "--reject-file=-", "--input", str(PATCHES[number]),
               "--forward"]
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
    assert not re.search(r"fuzz|offset|FAILED|reversed|previously applied|skipping", output, re.I), output


def without_comments(source):
    return b"".join(line for line in source.splitlines(keepends=True)
                    if not line.lstrip().startswith(b"//"))


def round_trip(directory, number, original):
    target = write_fixture(directory, number, original)
    assert_strict(apply_patch(directory, number, dry_run=True))
    assert target.read_bytes() == original
    assert_strict(apply_patch(directory, number))
    patched = target.read_bytes()
    assert patched != original
    assert without_comments(patched) == without_comments(original)
    assert apply_patch(directory, number, dry_run=True).returncode != 0
    assert target.read_bytes() == patched
    assert_strict(apply_patch(directory, number, reverse=True, dry_run=True))
    assert target.read_bytes() == patched
    assert_strict(apply_patch(directory, number, reverse=True))
    assert target.read_bytes() == original
    assert apply_patch(directory, number, reverse=True, dry_run=True).returncode != 0
    assert target.read_bytes() == original
    assert not list(directory.rglob("*.orig"))
    assert not list(directory.rglob("*.rej"))
    return patched


@pytest.fixture(scope="module")
def patched_sources(tmp_path_factory):
    directory = tmp_path_factory.mktemp("storage-sources")
    return {number: round_trip(directory, number, native_fixture(number)) for number in TARGETS}


@pytest.mark.parametrize("number", TARGETS)
def test_fixture_applies_without_fuzz_or_offset_and_reverses(patched_sources, number):
    assert without_comments(patched_sources[number]) == without_comments(native_fixture(number))


@pytest.mark.parametrize("number", TARGETS)
def test_optional_real_baseline_provenance_and_round_trip(tmp_path, number):
    baseline = os.environ.get("CHROMIX_STORAGE_BASELINE_ROOT")
    if not baseline:
        pytest.skip("set CHROMIX_STORAGE_BASELINE_ROOT to verify independent source provenance")
    source = Path(baseline) / TARGETS[number]
    original = source.read_bytes()
    assert hashlib.sha256(original).hexdigest() == SOURCE_SHA256[number]
    lines = original.decode().splitlines(keepends=True)
    for first, text in SOURCE_SECTIONS[number]:
        assert "".join(lines[first - 1:first - 1 + len(text.splitlines())]) == text
    patched = round_trip(tmp_path, number, original)
    index = re.search(r"^index ([0-9a-f]+)\.\.([0-9a-f]+) 100644$",
                      PATCHES[number].read_text(), re.M)
    assert index
    for data, expected in zip((original, patched), index.groups()):
        blob = hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()
        assert blob.startswith(expected)
    assert source.read_bytes() == original


def test_numbered_series_is_complete_and_contiguous():
    entries = [line.strip() for line in (ROOT / "patches/series").read_text().splitlines()
               if line.strip() and not line.lstrip().startswith("#")]
    assert entries == sorted(path.relative_to(ROOT).as_posix()
                             for path in (ROOT / "patches").glob("*.patch"))
    names = [Path(entry).name for entry in entries]
    assert [name[:4] for name in names] == [f"{number:04d}" for number in range(1, len(entries) + 1)]
    for number, path in PATCHES.items():
        assert names[int(number) - 1] == path.name


@pytest.mark.parametrize("number", TARGETS)
def test_retirement_is_only_a_short_constraint_comment(number):
    patch = PATCHES[number].read_text()
    assert re.findall(r"^\+\+\+ b/(.+)$", patch, re.M) == [TARGETS[number]]
    assert re.findall(r"^--- a/(.+)$", patch, re.M) == [TARGETS[number]]
    assert len(re.findall(r"^diff --git ", patch, re.M)) == 1
    assert len(re.findall(r"^@@ ", patch, re.M)) == 1
    additions = [line[1:] for line in patch.splitlines()
                 if line.startswith("+") and not line.startswith("+++")]
    assert 1 <= len(additions) <= 2
    assert all(line.strip().startswith("//") and not line.endswith("\\") for line in additions)
    assert not any(line.startswith("-") and not line.startswith("---") for line in patch.splitlines())
    for retired in ("uxr-storage-quota", "uxr-canvas-seed", "UxrConfig", "StringToUint",
                    "static_cast<int64_t>", "128ull", "2654435761", "ph_q", "ph_seed"):
        assert retired not in patch


@pytest.mark.parametrize("number", TARGETS)
@pytest.mark.parametrize("reverse", [False, True], ids=["forward", "reverse"])
@pytest.mark.parametrize("edge", [0, 5], ids=["leading-context", "trailing-context"])
def test_fuzz_required_context_is_rejected(tmp_path, number, reverse, edge):
    target = write_fixture(tmp_path, number)
    if reverse:
        assert_strict(apply_patch(tmp_path, number))
    lines = target.read_bytes().splitlines(keepends=True)
    position = HUNK_STARTS[number] - 1 + edge + (1 if reverse and edge == 5 else 0)
    lines[position] = b"  // incompatible native context\n"
    damaged = b"".join(lines)
    target.write_bytes(damaged)
    result = apply_patch(tmp_path, number, reverse=reverse, dry_run=True)
    assert result.returncode != 0, result.stdout + result.stderr
    assert target.read_bytes() == damaged


@pytest.mark.parametrize("number", TARGETS)
@pytest.mark.parametrize("reverse", [False, True], ids=["forward", "reverse"])
@pytest.mark.parametrize("shift", [-1, 1])
def test_zero_exit_offset_is_not_accepted(tmp_path, number, reverse, shift):
    target = write_fixture(tmp_path, number)
    if reverse:
        assert_strict(apply_patch(tmp_path, number))
    data = target.read_bytes()
    shifted = b"// shifted source\n" + data if shift == 1 else data.split(b"\n", 1)[1]
    target.write_bytes(shifted)
    result = apply_patch(tmp_path, number, reverse=reverse, dry_run=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "offset" in (result.stdout + result.stderr).lower()
    with pytest.raises(AssertionError, match="offset"):
        assert_strict(result)
    assert target.read_bytes() == shifted


@pytest.mark.parametrize("channel", ["stdout", "stderr"])
@pytest.mark.parametrize("diagnostic", ["with fuzz 1", "offset -1 lines", "FAILED", "Reversed patch"])
def test_strict_check_rejects_diagnostics_on_either_stream(channel, diagnostic):
    streams = {"stdout": "", "stderr": ""}
    streams[channel] = diagnostic
    with pytest.raises(AssertionError):
        assert_strict(subprocess.CompletedProcess([], 0, **streams))


def function(source, signature):
    start = source.index(signature)
    position = source.index("{", start) + 1
    depth = 1
    while depth:
        depth += (source[position] == "{") - (source[position] == "}")
        position += 1
    return source[start:position]


@pytest.mark.parametrize("number", TARGETS)
def test_native_dispatch_and_context_guards_are_unchanged(patched_sources, number):
    owner = "StorageManager" if number == "0034" else "StorageBucket"
    signature = f"ScriptPromise<StorageEstimate> {owner}::estimate("
    assert function(patched_sources[number].decode(), signature) == function(
        native_fixture(number).decode(), signature)


@pytest.fixture(scope="module", params=["native", "patched"])
def runtime_binary(request, tmp_path_factory, patched_sources):
    if not CXX:
        pytest.skip("a local C++20 compiler is required for executable storage stubs")
    sources = patched_sources if request.param == "patched" else {
        number: native_fixture(number) for number in TARGETS}
    manager = sources["0034"].decode()
    constants = manager[manager.index("const char kGenericErrorMessage[]"):
                        manager.index("void QueryStorageUsageAndQuotaCallback(")]
    callbacks = (function(manager, "void QueryStorageUsageAndQuotaCallback(") + "\n" +
                 function(sources["0039"].decode(), "void StorageBucket::DidGetEstimate("))
    directory = tmp_path_factory.mktemp(f"storage-runtime-{request.param}")
    source = directory / "storage.cc"
    source.write_text(CPP_SUPPORT + constants + callbacks + CPP_TESTS)
    binary = directory / "storage"
    result = subprocess.run([CXX, "-std=c++20", "-O0", "-Wall", "-Wextra", "-Werror",
                             str(source), "-o", str(binary)], text=True, capture_output=True,
                            timeout=60, env={**os.environ, "TMPDIR": str(directory)})
    assert result.returncode == 0, result.stdout + result.stderr
    return binary


@pytest.mark.parametrize("case", ["zero", "large-values", "quota-below-usage", "bucket-isolation",
                                  "usage-breakdown", "manager-errors", "bucket-errors",
                                  "signed-conversion", "random-values"])
def test_executable_native_storage_contract(runtime_binary, case):
    result = subprocess.run([str(runtime_binary), case], text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == f"PASS {case}\n"


@pytest.mark.parametrize("status", ["not-supported", "invalid-modification", "invalid-access"])
def test_unexpected_status_keeps_native_notreached(runtime_binary, status):
    result = subprocess.run([str(runtime_binary), status], text=True, capture_output=True, timeout=10)
    assert result.returncode == 86, result.stdout + result.stderr
    assert result.stderr == "NOTREACHED\n"


CPP_SUPPORT = r'''
#include <array>
#include <cassert>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <limits>
#include <memory>
#include <optional>
#include <random>
#include <string>
#include <utility>
#include <vector>

struct HeapObject { virtual ~HeapObject() = default; };
std::vector<std::unique_ptr<HeapObject>> heap;
template <typename T, typename... Args>
T* MakeGarbageCollected(Args&&... args) {
  auto object = std::make_unique<T>(std::forward<Args>(args)...);
  T* result = object.get();
  heap.push_back(std::move(object));
  return result;
}

struct StorageUsageDetails : HeapObject {
  std::optional<uint64_t> indexed_db, caches, service_workers, file_system;
  static StorageUsageDetails* Create() { return MakeGarbageCollected<StorageUsageDetails>(); }
  void setIndexedDB(uint64_t value) { indexed_db = value; }
  void setCaches(uint64_t value) { caches = value; }
  void setServiceWorkerRegistrations(uint64_t value) { service_workers = value; }
  void setFileSystem(uint64_t value) { file_system = value; }
};
struct StorageEstimate : HeapObject {
  std::optional<uint64_t> usage, quota;
  StorageUsageDetails* details = nullptr;
  static StorageEstimate* Create() { return MakeGarbageCollected<StorageEstimate>(); }
  void setUsage(uint64_t value) { usage = value; }
  void setQuota(uint64_t value) { quota = value; }
  void setUsageDetails(StorageUsageDetails* value) { details = value; }
};
enum class DOMExceptionCode { kUnknownError };
struct DOMException : HeapObject {
  DOMExceptionCode code;
  std::string message;
  DOMException(DOMExceptionCode value, const char* text) : code(value), message(text) {}
};
struct TypeError { std::string message; };
struct ScriptState { void* GetIsolate() { return nullptr; } };
struct V8ThrowException {
  static TypeError CreateTypeError(void*, const char* message) { return {message}; }
};
template <typename T>
struct ScriptPromiseResolver {
  ScriptState state;
  T* result = nullptr;
  unsigned resolved = 0, rejected = 0;
  std::optional<TypeError> type_error;
  DOMException* dom_error = nullptr;
  ScriptState* GetScriptState() { return &state; }
  void Resolve(T* value) {
    assert(resolved + rejected == 0);
    ++resolved;
    result = value;
  }
  void Reject(TypeError value) {
    assert(resolved + rejected == 0);
    ++rejected;
    type_error = value;
  }
  void Reject(DOMException* value) {
    assert(resolved + rejected == 0);
    ++rejected;
    dom_error = value;
  }
};
namespace mojom::blink {
enum class QuotaStatusCode {
  kOk = 0, kErrorNotSupported = 9, kErrorInvalidModification = 13,
  kErrorInvalidAccess = 15, kErrorAbort = 20, kUnknown = -1
};
struct UsageBreakdown {
  int64_t fileSystem = 0, webSql = 0, indexedDatabase = 0;
  int64_t serviceWorkerCache = 0, serviceWorker = 0, backgroundFetch = 0;
};
using UsageBreakdownPtr = std::unique_ptr<UsageBreakdown>;
}
using mojom::blink::QuotaStatusCode;
using mojom::blink::UsageBreakdown;
using mojom::blink::UsageBreakdownPtr;
using Resolver = ScriptPromiseResolver<StorageEstimate>;
struct StorageBucket {
  void DidGetEstimate(Resolver*, int64_t, int64_t, bool);
};
// Unexpected IPC statuses are fatal in Chromium, not recoverable TypeErrors.
[[noreturn]] void Unreachable() {
  std::fputs("NOTREACHED\n", stderr);
  std::exit(86);
}
#define NOTREACHED() Unreachable()
'''

CPP_TESTS = r'''
constexpr int64_t kMax = std::numeric_limits<int64_t>::max();
constexpr int64_t kGiB = int64_t{1} << 30;

void CheckEstimate(const Resolver& resolver, int64_t usage, int64_t quota) {
  assert(resolver.resolved == 1 && resolver.rejected == 0);
  assert(!resolver.type_error && resolver.dom_error == nullptr);
  assert(resolver.result && resolver.result->details);
  assert(resolver.result->usage == static_cast<uint64_t>(usage));
  assert(resolver.result->quota == static_cast<uint64_t>(quota));
}
void CheckDetails(const StorageUsageDetails& details, const UsageBreakdown& input) {
  const auto field = [](const std::optional<uint64_t>& actual, int64_t expected) {
    assert(actual.has_value() == (expected != 0));
    if (actual) assert(*actual == static_cast<uint64_t>(expected));
  };
  field(details.indexed_db, input.indexedDatabase);
  field(details.caches, input.serviceWorkerCache);
  field(details.service_workers, input.serviceWorker);
  field(details.file_system, input.fileSystem);
}
Resolver Manager(int64_t usage, int64_t quota, UsageBreakdown breakdown = {}) {
  Resolver resolver;
  const auto allocations = heap.size();
  QueryStorageUsageAndQuotaCallback(&resolver, QuotaStatusCode::kOk, usage, quota,
                                    std::make_unique<UsageBreakdown>(breakdown));
  CheckEstimate(resolver, usage, quota);
  CheckDetails(*resolver.result->details, breakdown);
  assert(heap.size() == allocations + 2);
  return resolver;
}
Resolver Bucket(StorageBucket& bucket, int64_t usage, int64_t quota) {
  Resolver resolver;
  const auto allocations = heap.size();
  bucket.DidGetEstimate(&resolver, usage, quota, true);
  CheckEstimate(resolver, usage, quota);
  CheckDetails(*resolver.result->details, {});
  assert(heap.size() == allocations + 2);
  return resolver;
}
void CheckPair(int64_t usage, int64_t quota) {
  StorageBucket bucket;
  Manager(usage, quota);
  Bucket(bucket, usage, quota);
}

int main(int argc, char** argv) {
  assert(argc == 2);
  const std::string test = argv[1];
  if (test == "zero") {
    CheckPair(0, 0);
    CheckPair(0, 10 * kGiB);
    CheckPair(4096, 10 * kGiB + 123);
  } else if (test == "large-values") {
    const std::array<int64_t, 12> values = {
      0, 1, (int64_t{1} << 32) - 1, int64_t{1} << 32,
      128 * kGiB, 224 * kGiB + 17, (int64_t{1} << 53) - 1,
      int64_t{1} << 53, (int64_t{1} << 53) + 1,
      int64_t{1} << 62, kMax - 1, kMax
    };
    for (const auto usage : values)
      for (const auto quota : values) CheckPair(usage, quota);
  } else if (test == "quota-below-usage") {
    CheckPair(1, 0);
    CheckPair(8192, 4096);
    CheckPair(kMax, 0);
    CheckPair(kMax, kMax - 1);
  } else if (test == "bucket-isolation") {
    StorageBucket first, second;
    const auto origin = Manager(9999, 10 * kGiB);
    const auto a = Bucket(first, 400, 1024);
    const auto b = Bucket(second, 500, 256);
    const auto later = Bucket(first, 800, 4096);
    Manager(1, kMax);
    const auto repeated = Bucket(second, 500, 256);
    CheckEstimate(origin, 9999, 10 * kGiB);
    CheckEstimate(a, 400, 1024);
    CheckEstimate(b, 500, 256);
    CheckEstimate(later, 800, 4096);
    CheckEstimate(repeated, 500, 256);
    assert(origin.result != a.result && a.result != b.result);
    assert(a.result->details != b.result->details);
    assert(later.result != a.result && repeated.result != b.result);
  } else if (test == "usage-breakdown") {
    for (unsigned mask = 0; mask < 16; ++mask) {
      UsageBreakdown input;
      input.indexedDatabase = (mask & 1) ? kMax : 0;
      input.serviceWorkerCache = (mask & 2) ? (int64_t{1} << 53) + 1 : 0;
      input.serviceWorker = (mask & 4) ? 37 : 0;
      input.fileSystem = (mask & 8) ? (int64_t{1} << 32) + 1 : 0;
      input.webSql = kMax;
      input.backgroundFetch = kMax;
      const auto result = Manager(7, 3, input);
      CheckEstimate(result, 7, 3);
      StorageBucket bucket;
      Bucket(bucket, 7, 3);
    }
    UsageBreakdown largest;
    largest.indexedDatabase = largest.serviceWorkerCache = kMax;
    largest.serviceWorker = largest.fileSystem = kMax;
    Manager(kMax, kMax, largest);
  } else if (test == "manager-errors") {
    for (const auto status : {QuotaStatusCode::kUnknown, QuotaStatusCode::kErrorAbort}) {
      for (const bool have_breakdown : {false, true}) {
        for (const auto value : {int64_t{0}, int64_t{-1}, kMax}) {
          Resolver resolver;
          const auto allocations = heap.size();
          QueryStorageUsageAndQuotaCallback(&resolver, status, value, value,
              have_breakdown ? std::make_unique<UsageBreakdown>() : nullptr);
          assert(resolver.rejected == 1 && resolver.resolved == 0 && !resolver.result);
          assert(resolver.type_error && !resolver.dom_error);
          assert(resolver.type_error->message == (status == QuotaStatusCode::kUnknown
              ? "Internal error when calculating storage usage."
              : "The operation was aborted due to shutdown."));
          assert(heap.size() == allocations);
        }
      }
    }
    Manager(1, 2);
  } else if (test == "bucket-errors") {
    StorageBucket bucket;
    const auto previous = Bucket(bucket, 100, 200);
    for (const auto value : {int64_t{0}, int64_t{-1}, kMax}) {
      Resolver resolver;
      const auto allocations = heap.size();
      bucket.DidGetEstimate(&resolver, value, value, false);
      assert(resolver.rejected == 1 && resolver.resolved == 0 && !resolver.result);
      assert(!resolver.type_error && resolver.dom_error);
      assert(resolver.dom_error->code == DOMExceptionCode::kUnknownError);
      assert(resolver.dom_error->message == "Unknown error occurred while getting estimate.");
      assert(heap.size() == allocations + 1);
    }
    CheckEstimate(previous, 100, 200);
    Bucket(bucket, 300, 400);
  } else if (test == "signed-conversion") {
    // Preserve native signed IPC to unsigned IDL conversion, not a new clamp.
    CheckPair(-1, -1);
    CheckPair(std::numeric_limits<int64_t>::min(), kMax);
    UsageBreakdown negative;
    negative.indexedDatabase = -1;
    negative.fileSystem = std::numeric_limits<int64_t>::min();
    Manager(-1, 0, negative);
  } else if (test == "random-values") {
    std::mt19937_64 random(0x534f52414745ULL);
    const auto next = [&]() { return static_cast<int64_t>(random() & uint64_t{kMax}); };
    StorageBucket first, second;
    for (unsigned i = 0; i < 2048; ++i) {
      const auto usage = next(), quota = next();
      UsageBreakdown details;
      details.indexedDatabase = i % 2 ? next() : 0;
      details.serviceWorkerCache = i % 3 ? next() : 0;
      details.serviceWorker = i % 5 ? next() : 0;
      details.fileSystem = i % 7 ? next() : 0;
      details.webSql = next();
      details.backgroundFetch = next();
      Manager(usage, quota, details);
      Bucket(i % 2 ? first : second, usage, quota);
    }
  } else if (test == "not-supported" || test == "invalid-modification" || test == "invalid-access") {
    const auto status = test == "not-supported" ? QuotaStatusCode::kErrorNotSupported
        : test == "invalid-modification" ? QuotaStatusCode::kErrorInvalidModification
        : QuotaStatusCode::kErrorInvalidAccess;
    Resolver resolver;
    QueryStorageUsageAndQuotaCallback(&resolver, status, kMax, kMax, nullptr);
    return 1;
  } else {
    return 2;
  }
  std::printf("PASS %s\n", test.c_str());
}
'''
