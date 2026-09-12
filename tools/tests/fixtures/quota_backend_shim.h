// Minimal callback/scheduler/database seams for the quota source contract test.
// This is NOT a Mojo, disk-allocation, Chromium ABI or browser implementation.
#include <functional>
#include <memory>
#include <set>
#include <tuple>
#include <type_traits>
#include <utility>

#define DCHECK_CALLED_ON_VALID_SEQUENCE(...) ((void)0)
#define CHECK(value, ...) assert(value)
#define UMA_HISTOGRAM_MBYTES(...) ((void)0)
#define UMA_HISTOGRAM_PERCENTAGE(...) ((void)0)

namespace base {
template <typename Signature> class OnceCallback;
template <typename R, typename... Args> class OnceCallback<R(Args...)> {
  struct Callable {
    virtual ~Callable() = default;
    virtual R Call(Args... args) = 0;
  };
  template <typename F> struct Impl : Callable {
    F function;
    explicit Impl(F value) : function(std::move(value)) {}
    R Call(Args... args) override {
      return std::invoke(function, std::forward<Args>(args)...);
    }
  };
  std::unique_ptr<Callable> callable_;
 public:
  OnceCallback() = default;
  OnceCallback(OnceCallback&&) = default;
  OnceCallback& operator=(OnceCallback&&) = default;
  template <typename F, std::enable_if_t<!std::is_same_v<std::decay_t<F>, OnceCallback>, int> = 0>
  OnceCallback(F function)
      : callable_(std::make_unique<Impl<F>>(std::move(function))) {}
  explicit operator bool() const { return bool(callable_); }
  R Run(Args... args) {
    assert(callable_);
    auto callable = std::move(callable_);
    return callable->Call(std::forward<Args>(args)...);
  }
};
template <typename F, typename... Bound>
auto BindOnce(F function, Bound... bound) {
  return [function = std::move(function), bound = std::make_tuple(std::move(bound)...)]
      (auto&&... rest) mutable -> decltype(auto) {
    return std::apply([&](auto&... first) -> decltype(auto) {
      return std::invoke(function, std::move(first)...,
                         std::forward<decltype(rest)>(rest)...);
    }, bound);
  };
}
template <typename T> struct WeakPtr {
  T* pointer;
  std::shared_ptr<bool> live;
  T* get() const { return *live ? pointer : nullptr; }
  explicit operator bool() const { return get() != nullptr; }
  T* operator->() const { assert(get()); return get(); }
};
template <typename T> struct WeakPtrFactory {
  T* pointer;
  std::shared_ptr<bool> live = std::make_shared<bool>(true);
  WeakPtr<T> GetWeakPtr() const { return {pointer, live}; }
  void InvalidateWeakPtrs() {
    *live = false;
    live = std::make_shared<bool>(true);
  }
};
template <typename T> class ConcurrentCallbacks {
  struct State {
    size_t pending = 0;
    std::vector<T> results;
    OnceCallback<void(std::vector<T>)> final;
    void MaybeDone() {
      if (!pending && final) std::move(final).Run(std::move(results));
    }
  };
  std::shared_ptr<State> state_ = std::make_shared<State>();
 public:
  OnceCallback<void(T)> CreateCallback() {
    ++state_->pending;
    return [state = state_](T value) {
      state->results.push_back(value);
      --state->pending;
      state->MaybeDone();
    };
  }
  void Done(OnceCallback<void(std::vector<T>)> final) {
    state_->final = std::move(final);
    state_->MaybeDone();
  }
};
template <typename T> struct Unexpected { T error; };
template <typename T> Unexpected<T> unexpected(T error) { return {error}; }
}

namespace blink::mojom {
enum class QuotaStatusCode { kOk, kUnknown, kErrorAbort };
struct UsageBreakdown { int serial = 7; };
using UsageBreakdownPtr = std::unique_ptr<UsageBreakdown>;
}
using blink::mojom::QuotaStatusCode;
using blink::mojom::UsageBreakdownPtr;
enum class QuotaError { kUnknownError };
template <typename T> class QuotaErrorOr {
  std::optional<T> value_;
 public:
  QuotaErrorOr(T value) : value_(std::move(value)) {}
  QuotaErrorOr(base::Unexpected<QuotaError>) {}
  bool has_value() const { return value_.has_value(); }
  const T& value() const { return value_.value(); }
};
struct StorageKey {
  int id = 1;
  StorageKey origin() const { return *this; }
  int GetURL() const { return id; }
  bool operator<(const StorageKey& other) const { return id < other.id; }
};
using BucketId = int;
struct BucketLocator { BucketId id; StorageKey storage_key; bool is_default; };
struct BucketInfo {
  BucketId id = 2;
  StorageKey storage_key;
  int64_t quota = 0;
};
struct QuotaSettings {
  int64_t per_storage_key_quota = 9000;
  int64_t session_only_per_storage_key_quota = 8000;
  int64_t must_remain_available = 100;
};
struct SpecialStoragePolicy {
  std::set<int> unlimited, session_only;
  bool IsStorageUnlimited(int key) const { return unlimited.contains(key); }
  bool IsStorageSessionOnly(int key) const { return session_only.contains(key); }
};
constexpr int64_t kNoLimit = std::numeric_limits<int64_t>::max();
constexpr int64_t kMiB = int64_t{1} << 20;
constexpr int64_t kGiB = int64_t{1} << 30;

class QuotaManagerImpl {
 public:
  using UsageAndQuotaCallback = base::OnceCallback<void(QuotaStatusCode, int64_t, int64_t)>;
  using UsageAndQuotaWithBreakdownCallback =
      base::OnceCallback<void(QuotaStatusCode, int64_t, int64_t, UsageBreakdownPtr)>;
  using UsageAndQuotaWithBreakdownAndOverrideFlagCallback =
      base::OnceCallback<void(QuotaStatusCode, int64_t, int64_t, bool, UsageBreakdownPtr)>;
  class UsageAndQuotaInfoGatherer;
  struct QuotaOverride { std::optional<int64_t> quota_size; };
  std::map<StorageKey, QuotaOverride> devtools_overrides_;
  std::unique_ptr<SpecialStoragePolicy> special_storage_policy_;
  base::WeakPtrFactory<QuotaManagerImpl> weak_factory_{this};
  bool report_static_storage_quota_ = true, is_incognito_ = false;
  bool defer_usage = false;
  QuotaSettings settings;
  int disk_queries = 0, origin_checks = 0, bucket_checks = 0;
  int64_t origin_usage = 100, bucket_usage = 25;
  int64_t disk_available = 80 * kGiB;
  std::map<BucketId, BucketInfo> buckets{{2, {}}};
  std::vector<base::OnceCallback<void()>> pending;

  void EnsureDatabaseOpened() {}
  bool IsStorageUnlimited(const StorageKey&) const;
  int64_t GetQuotaForStorageKey(const StorageKey&, const QuotaSettings&) const;
  std::optional<int64_t> GetQuotaOverrideForStorageKey(const StorageKey&);
  void GetUsageAndQuotaWithBreakdown(const StorageKey&, UsageAndQuotaWithBreakdownCallback);
  void GetUsageAndReportedQuotaWithBreakdown(const StorageKey&, UsageAndQuotaWithBreakdownCallback);
  void HandleGetUsageAndQuotaRequest(const StorageKey&, UsageAndQuotaWithBreakdownAndOverrideFlagCallback);
  void GetUsageAndQuota(const StorageKey&, UsageAndQuotaCallback);
  void GetBucketUsageAndQuota(BucketId, UsageAndQuotaCallback);
  void GetBucketUsageAndReportedQuota(BucketId, UsageAndQuotaCallback);
  void GetBucketSpaceRemaining(const BucketLocator&, base::OnceCallback<void(QuotaErrorOr<int64_t>)>);
  void GetBucketById(BucketId id, base::OnceCallback<void(QuotaErrorOr<BucketInfo>)> callback) {
    if (buckets.contains(id)) std::move(callback).Run(buckets.at(id));
    else std::move(callback).Run(base::unexpected(QuotaError::kUnknownError));
  }
  void GetDiskAvailabilityAndTempPoolSize(base::OnceCallback<void(int64_t, int64_t, int64_t)> callback) {
    ++disk_queries;
    std::move(callback).Run(100 * kGiB, disk_available, 90 * kGiB);
  }
  void RunPending() {
    auto tasks = std::move(pending);
    pending.clear();
    for (auto& task : tasks) std::move(task).Run();
  }
};

// The fake disk is deliberately fixed: tests verify native static dispatch,
// not Chromium's disk rounding, pressure eviction or actual allocations.
int64_t CalculateReportedQuota(int64_t, int64_t usage, bool) { return usage + 10 * kGiB; }
struct QuotaTask {
  virtual ~QuotaTask() = default;
  virtual void Completed() = 0;
};
class QuotaManagerImpl::UsageAndQuotaInfoGatherer : public QuotaTask {
  QuotaManagerImpl* owner_;
  StorageKey storage_key_;
  std::optional<BucketInfo> bucket_info_;
  UsageAndQuotaWithBreakdownAndOverrideFlagCallback callback_;
  bool is_unlimited_, is_incognito_;
  int64_t desired_storage_key_quota_ = 0, usage_ = 0, available_space_ = 0;
  UsageBreakdownPtr usage_breakdown_ = std::make_unique<blink::mojom::UsageBreakdown>();
  QuotaSettings settings_;
  base::WeakPtrFactory<UsageAndQuotaInfoGatherer> weak_factory_{this};
  QuotaManagerImpl* manager() const { return owner_; }
  void DeleteSoon() { delete this; }
 public:
  UsageAndQuotaInfoGatherer(QuotaManagerImpl* manager, const StorageKey& key, bool incognito,
                           UsageAndQuotaWithBreakdownAndOverrideFlagCallback callback)
      : owner_(manager), storage_key_(key), callback_(std::move(callback)),
        is_unlimited_(manager->IsStorageUnlimited(key)), is_incognito_(incognito) {}
  UsageAndQuotaInfoGatherer(QuotaManagerImpl* manager, const BucketInfo& bucket, bool incognito,
                           UsageAndQuotaWithBreakdownAndOverrideFlagCallback callback)
      : UsageAndQuotaInfoGatherer(manager, bucket.storage_key, incognito, std::move(callback)) {
    bucket_info_ = bucket;
  }
  void Start() {
    if (bucket_info_) ++owner_->bucket_checks; else ++owner_->origin_checks;
    settings_ = owner_->settings;
    desired_storage_key_quota_ = owner_->GetQuotaForStorageKey(storage_key_, settings_);
    available_space_ = owner_->disk_available;
    base::OnceCallback<void()> finish = [this] {
      usage_ = bucket_info_ ? owner_->bucket_usage : owner_->origin_usage;
      Completed();
    };
    if (owner_->defer_usage) owner_->pending.push_back(std::move(finish));
    else std::move(finish).Run();
  }
// @GATHERER_COMPLETED@
};
