"""Execute quota patch methods using pinned source excerpts and callback shims.

GNU patch must round-trip those independent excerpts without offsets or fuzz.
The executable checks control flow; it is not a native Chromium/browser build,
Mojo test, filesystem quota test, or proof the complete patch stack applies.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import shutil
import subprocess

import pytest

from test_fingerprint_features import CPP_BASE, ROOT, block, compile_cpp

FIXTURES = Path(__file__).with_name('fixtures')
PATCH = ROOT / 'patches/0129-storage-quota-backend.patch'
TARGET = 'storage/browser/quota/quota_manager_impl.cc'
SOURCE_SHA256 = 'ecbb45b70eb7ddca3356ef0529f1da165e8440831fd9ad72c0f351ca1bedc4f6'


def source_sections():
    fixture = (FIXTURES / 'quota_manager_152_sections.cc').read_text(encoding='utf-8')
    sections = re.findall(r'^// CHROMIUM_SOURCE_SECTION (\d+)\n(.*?)^// CHROMIUM_SOURCE_END\n',
                          fixture, flags=re.M | re.S)
    return [(int(first), source) for first, source in sections]


def native_source():
    lines = []
    for first, source in source_sections():
        assert len(lines) < first
        lines.extend('// unrelated native source\n' for _ in range(first - 1 - len(lines)))
        lines.extend(source.splitlines(keepends=True))
    return ''.join(lines)


def apply_quota_patch(directory, *, reverse=False):
    command = shutil.which('gpatch') or shutil.which('patch')
    if not command:
        pytest.skip('GNU patch is required')
    result = subprocess.run([
        command, '-p1', '--fuzz=0', '--batch', '--binary', '--get=0',
        '--no-backup-if-mismatch', '--reject-file=-', '--input', str(PATCH),
        '--reverse' if reverse else '--forward',
    ], cwd=directory, capture_output=True, text=True, timeout=15,
        env={**os.environ, 'LC_ALL': 'C', 'PATCH_GET': '0'})
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert not re.search(r'offset|fuzz|FAILED|Reversed patch', output, re.I), output


@pytest.fixture(scope='module')
def patched_source(tmp_path_factory):
    directory = tmp_path_factory.mktemp('quota-source-roundtrip')
    target = directory / TARGET
    target.parent.mkdir(parents=True)
    original = native_source().encode()
    target.write_bytes(original)
    apply_quota_patch(directory)
    patched = target.read_bytes()
    apply_quota_patch(directory, reverse=True)
    assert target.read_bytes() == original
    return patched.decode()


def test_quota_patch_round_trips_independent_sections(patched_source):
    assert 'FingerprintStorageQuotaBytes()' in patched_source


def test_optional_quota_source_provenance():
    baseline = os.environ.get('CHROMIX_QUOTA_BASELINE_ROOT')
    if not baseline:
        pytest.skip('set CHROMIX_QUOTA_BASELINE_ROOT to verify the independent source hash')
    original = (Path(baseline) / TARGET).read_bytes()
    assert hashlib.sha256(original).hexdigest() == SOURCE_SHA256
    lines = original.decode().splitlines(keepends=True)
    for first, source in source_sections():
        assert ''.join(lines[first - 1:first - 1 + len(source.splitlines())]) == source


@pytest.fixture(scope='module', params=['native', 'patched'])
def quota_contract(request, tmp_path_factory, patched_source):
    source = patched_source if request.param == 'patched' else native_source()
    shim = (FIXTURES / 'quota_backend_shim.h').read_text(encoding='utf-8')
    shim = shim.replace('// @GATHERER_COMPLETED@', block(source, '  void Completed() override {'))
    # Chromium intentionally permits unused callback parameters. Keep other
    # -Wall/-Wextra/-Werror checks active in the portable contract compile.
    header = '#if defined(__clang__) || defined(__GNUC__)\n#pragma GCC diagnostic ignored "-Wunused-parameter"\n#endif\n'
    definitions = ''
    if request.param == 'patched':
        definitions += block(source, 'std::optional<int64_t> FingerprintStorageQuotaBytes()') + '\n'
    for signature in [
        'void DidGetUsageAndQuotaStripBreakdown(', 'void DidGetUsageAndQuotaStripOverride(',
        'void QuotaManagerImpl::GetUsageAndQuotaWithBreakdown(',
        'void QuotaManagerImpl::GetUsageAndReportedQuotaWithBreakdown(',
        'void QuotaManagerImpl::HandleGetUsageAndQuotaRequest(',
        'void QuotaManagerImpl::GetUsageAndQuota(',
        'void QuotaManagerImpl::GetBucketUsageAndReportedQuota(',
        'void QuotaManagerImpl::GetBucketSpaceRemaining(',
        'bool QuotaManagerImpl::IsStorageUnlimited(',
        'int64_t QuotaManagerImpl::GetQuotaForStorageKey(',
        'std::optional<int64_t> QuotaManagerImpl::GetQuotaOverrideForStorageKey(',
    ]:
        definitions += block(source, signature) + '\n'
    binary = compile_cpp(tmp_path_factory.mktemp('quota-backend-' + request.param),
                         header + CPP_BASE + shim + definitions + CPP_TESTS)
    return request.param, binary


@pytest.mark.parametrize('case', [
    'native-policy', 'native-reporting', 'native-space', 'native-zero-sentinel',
    'errors', 'missing-bucket', 'override-policy', 'override-reporting',
    'zero-override', 'override-space', 'origin-limit', 'callback-races', 'unlimited',
])
def test_executable_quota_backend_contract(quota_contract, case):
    mode, binary = quota_contract
    if mode == 'native' and case in {
        'override-policy', 'override-reporting', 'zero-override', 'override-space',
        'origin-limit', 'callback-races', 'unlimited',
    }:
        pytest.skip('new public-switch behavior is tested on the patched source only')
    result = subprocess.run([str(binary), case], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == f'PASS {case}\n'


CPP_TESTS = r'''
void QuotaManagerImpl::GetBucketUsageAndQuota(BucketId id, UsageAndQuotaCallback callback) {
  GetBucketById(id, [this, callback = std::move(callback)](QuotaErrorOr<BucketInfo> result) mutable {
    if (!result.has_value()) {
      std::move(callback).Run(QuotaStatusCode::kUnknown, 0, 0);
      return;
    }
    UsageAndQuotaWithBreakdownCallback strip =
        base::BindOnce(&DidGetUsageAndQuotaStripBreakdown, std::move(callback));
    auto* helper = new UsageAndQuotaInfoGatherer(this, result.value(), is_incognito_,
        base::BindOnce(&DidGetUsageAndQuotaStripOverride, std::move(strip)));
    helper->Start();
  });
}
struct Estimate { QuotaStatusCode status; int64_t usage, quota; };
void SetPublicQuota(const std::string& value) {
  auto& flags = base::CommandLine::ForCurrentProcess()->values;
  flags.erase("uxr-storage-quota");
  if (!value.empty()) flags["uxr-storage-quota"] = value;
}
Estimate Origin(QuotaManagerImpl& manager) {
  std::optional<Estimate> result;
  manager.GetUsageAndReportedQuotaWithBreakdown({}, [&](QuotaStatusCode status, int64_t usage,
                                                      int64_t quota, UsageBreakdownPtr breakdown) {
    assert(!result && breakdown && breakdown->serial == 7);
    result = {status, usage, quota};
  });
  assert(result);
  return *result;
}
Estimate Bucket(QuotaManagerImpl& manager) {
  std::optional<Estimate> result;
  manager.GetBucketUsageAndReportedQuota(2, [&](QuotaStatusCode status, int64_t usage, int64_t quota) {
    assert(!result); result = {status, usage, quota};
  });
  assert(result);
  return *result;
}
QuotaErrorOr<int64_t> Space(QuotaManagerImpl& manager, bool is_default = false) {
  std::optional<QuotaErrorOr<int64_t>> result;
  manager.GetBucketSpaceRemaining({is_default ? 1 : 2, {}, is_default},
      [&](QuotaErrorOr<int64_t> space) { assert(!result); result = space; });
  assert(result);
  return *result;
}
void Check(const Estimate& result, int64_t usage, int64_t quota) {
  assert(result.status == QuotaStatusCode::kOk);
  assert(result.usage == usage && result.quota == quota);
}
int main(int argc, char** argv) {
  assert(argc == 2);
  const std::string test = argv[1];
  SetPublicQuota("");
  QuotaManagerImpl manager;
  const StorageKey key{};
  if (test == "native-policy") {
    assert(!manager.GetQuotaOverrideForStorageKey(key));
    assert(manager.GetQuotaForStorageKey(key, manager.settings) == 9000);
    manager.special_storage_policy_ = std::make_unique<SpecialStoragePolicy>();
    manager.special_storage_policy_->session_only.insert(1);
    assert(manager.GetQuotaForStorageKey(key, manager.settings) == 8000);
    manager.special_storage_policy_->unlimited.insert(1);
    assert(manager.GetQuotaForStorageKey(key, manager.settings) == kNoLimit);
  } else if (test == "native-reporting") {
    for (bool static_quota : {false, true}) {
      manager.report_static_storage_quota_ = static_quota;
      for (bool incognito : {false, true}) {
        manager.is_incognito_ = incognito;
        Check(Origin(manager), 100, static_quota ? 100 + 10 * kGiB : 9000);
        for (int64_t cap : {int64_t{0}, int64_t{1000}, int64_t{12000}}) {
          manager.buckets.at(2).quota = cap;
          const int64_t expected = static_quota ? (cap ? cap : 25 + 10 * kGiB)
                                               : (cap ? std::min(cap, int64_t{9000}) : 9000);
          Check(Bucket(manager), 25, expected);
        }
      }
    }
  } else if (test == "native-space") {
    assert(Space(manager, true).value() == 8900);
    assert(manager.origin_checks == 1 && manager.bucket_checks == 0);
    manager.buckets.at(2).quota = 1000;
    assert(Space(manager).value() == 975);
    assert(manager.origin_checks == 2 && manager.bucket_checks == 1);
    manager.origin_usage = 8999;
    assert(Space(manager).value() == 1);
    manager.origin_usage = 9001;
    assert(Space(manager).value() == -1);
  } else if (test == "native-zero-sentinel") {
    manager.settings.per_storage_key_quota = 0;
    assert(Space(manager, true).value() == kNoLimit);
    assert(Space(manager).value() == kNoLimit);
  } else if (test == "errors") {
    for (bool static_quota : {false, true}) {
      manager.report_static_storage_quota_ = static_quota;
      manager.origin_usage = manager.bucket_usage = -1;
      assert(Origin(manager).status == QuotaStatusCode::kUnknown);
      assert(Bucket(manager).status == QuotaStatusCode::kUnknown);
      assert(!Space(manager, true).has_value() && !Space(manager).has_value());
    }
  } else if (test == "missing-bucket") {
    manager.buckets.clear();
    for (bool static_quota : {false, true}) {
      manager.report_static_storage_quota_ = static_quota;
      auto result = Bucket(manager);
      assert(result.status == QuotaStatusCode::kUnknown && result.quota == 0);
      assert(!Space(manager).has_value());
      assert(Space(manager, true).has_value());
    }
  } else if (test == "override-policy") {
    SetPublicQuota("1");
    assert(manager.GetQuotaForStorageKey(key, manager.settings) == kMiB);
    assert(manager.GetQuotaOverrideForStorageKey(key) == kMiB);
    manager.devtools_overrides_[key].quota_size = 500;
    assert(manager.GetQuotaOverrideForStorageKey(key) == 500);
    assert(manager.GetQuotaOverrideForStorageKey({2}) == kMiB);
    manager.devtools_overrides_[key].quota_size = 0;
    assert(manager.GetQuotaOverrideForStorageKey(key) == 0);
    manager.devtools_overrides_.erase(key);
    assert(manager.GetQuotaOverrideForStorageKey(key) == kMiB);
    SetPublicQuota("invalid");
    assert(!manager.GetQuotaOverrideForStorageKey(key));
    assert(manager.GetQuotaForStorageKey(key, manager.settings) == 9000);
  } else if (test == "override-reporting") {
    SetPublicQuota("1");
    for (bool static_quota : {false, true}) {
      manager.report_static_storage_quota_ = static_quota;
      for (bool incognito : {false, true}) {
        manager.is_incognito_ = incognito;
        for (int64_t effective : {int64_t{0}, int64_t{500}, kMiB, kMiB * 2}) {
          manager.devtools_overrides_[key].quota_size = effective;
          Check(Origin(manager), 100, effective);
          for (int64_t cap : {int64_t{0}, int64_t{300}, kMiB * 4}) {
            manager.buckets.at(2).quota = cap;
            Check(Bucket(manager), 25, cap ? std::min(cap, effective) : effective);
          }
        }
        manager.devtools_overrides_.clear();
        manager.buckets.at(2).quota = 2 * kMiB;
        Check(Origin(manager), 100, kMiB);
        Check(Bucket(manager), 25, kMiB);
      }
    }
    assert(manager.disk_queries == 0);
    manager.origin_usage = manager.bucket_usage = -1;
    auto origin = Origin(manager), bucket = Bucket(manager);
    assert(origin.status == QuotaStatusCode::kUnknown && origin.usage == 0 && origin.quota == 0);
    assert(bucket.status == QuotaStatusCode::kUnknown && bucket.usage == 0 && bucket.quota == 0);
  } else if (test == "zero-override") {
    for (bool devtools : {false, true}) {
      SetPublicQuota(devtools ? "1" : "0");
      if (devtools) manager.devtools_overrides_[key].quota_size = 0;
      manager.origin_usage = manager.bucket_usage = 0;
      Check(Origin(manager), 0, 0); Check(Bucket(manager), 0, 0);
      assert(Space(manager, true).value() == 0 && Space(manager).value() == 0);
      manager.origin_usage = 100; manager.bucket_usage = 25;
      assert(Space(manager, true).value() == -100 && Space(manager).value() == -100);
      manager.devtools_overrides_.clear();
    }
  } else if (test == "override-space") {
    SetPublicQuota("1");
    assert(Space(manager, true).value() == kMiB - 100);
    manager.buckets.at(2).quota = 200;
    assert(Space(manager).value() == 175);
    manager.devtools_overrides_[key].quota_size = 50;
    assert(Space(manager).value() == -50);
    manager.origin_usage = -1;
    assert(!Space(manager).has_value());
  } else if (test == "origin-limit") {
    SetPublicQuota("1");
    // A mostly empty bucket cannot evade usage allocated in sibling buckets.
    manager.buckets.at(2).quota = 2 * kMiB;
    manager.origin_usage = kMiB - 1;
    Check(Bucket(manager), 25, kMiB);
    assert(Space(manager).value() == 1);
    manager.origin_usage = kMiB + 1;
    assert(Space(manager).value() == -1);
  } else if (test == "callback-races") {
    manager.defer_usage = true;
    // The override is installed after scheduling, before gatherer completion.
    std::optional<Estimate> result;
    manager.GetUsageAndReportedQuotaWithBreakdown(key,
        [&](QuotaStatusCode status, int64_t usage, int64_t quota, UsageBreakdownPtr) {
          assert(!result); result = {status, usage, quota};
        });
    assert(!result);
    manager.devtools_overrides_[key].quota_size = 0;
    manager.RunPending();
    Check(*result, 100, 0);
    assert(manager.disk_queries == 0);
    for (bool install : {false, true}) {
      manager.settings.per_storage_key_quota = 0;
      manager.devtools_overrides_.clear();
      if (!install) manager.devtools_overrides_[key].quota_size = 0;
      std::optional<QuotaErrorOr<int64_t>> space;
      manager.GetBucketSpaceRemaining({1, key, true},
          [&](QuotaErrorOr<int64_t> value) { assert(!space); space = value; });
      assert(!space);
      if (install) manager.devtools_overrides_[key].quota_size = 0;
      else manager.devtools_overrides_.clear();
      manager.RunPending();
      assert(space && space->value() == (install ? -100 : kNoLimit));
    }
    // Teardown during an async request must not dereference an expired manager.
    std::optional<QuotaErrorOr<int64_t>> space;
    manager.GetBucketSpaceRemaining({2, key, false},
        [&](QuotaErrorOr<int64_t> value) { assert(!space); space = value; });
    manager.weak_factory_.InvalidateWeakPtrs();
    manager.RunPending();
    assert(space && !space->has_value());
  } else if (test == "unlimited") {
    SetPublicQuota("0");
    manager.special_storage_policy_ = std::make_unique<SpecialStoragePolicy>();
    manager.special_storage_policy_->unlimited.insert(1);
    assert(!manager.GetQuotaOverrideForStorageKey(key));
    assert(manager.GetQuotaForStorageKey(key, manager.settings) == kNoLimit);
    Check(Origin(manager), 100, manager.disk_available + 100);
    assert(Space(manager, true).value() == kNoLimit);
    manager.devtools_overrides_[key].quota_size = 4096;
    assert(manager.GetQuotaOverrideForStorageKey(key) == 4096);
    Check(Origin(manager), 100, 4096);
    // Preserve the native privileged allocation bypass for DevTools, too.
    assert(Space(manager, true).value() == kNoLimit);
  } else {
    return 2;
  }
  std::cout << "PASS " << test << '\n';
}
'''
