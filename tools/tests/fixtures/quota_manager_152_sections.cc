// Copyright 2013 The Chromium Authors
// Use of this source code is governed by a BSD-style license that can be
// found in the Chromium LICENSE file.
// Chromium 152.0.7977.82 quota_manager_impl.cc source excerpts.
// Copied from the read-only local reference, not derived from patch hunks.
// These excerpts are a callback contract fixture, not a clean-stack receipt.
// Full reference file SHA256: ecbb45b70eb7ddca3356ef0529f1da165e8440831fd9ad72c0f351ca1bedc4f6

// CHROMIUM_SOURCE_SECTION 80
namespace storage {

namespace {

// These values are used in UMA, so the list should be append-only.
enum class DatabaseDisabledReason {
  kRegisterStorageKeyFailed = 0,
  kSetIsBootstrappedFailed = 1,
  kRazeFailed = 2,
  kMaxValue = kRazeFailed,
};
// CHROMIUM_SOURCE_END

// CHROMIUM_SOURCE_SECTION 109
  base::UmaHistogramEnumeration("Quota.QuotaDatabaseDisabled", reason);
}

void DidGetUsageAndQuotaStripBreakdown(
    QuotaManagerImpl::UsageAndQuotaCallback callback,
    blink::mojom::QuotaStatusCode status,
    int64_t usage,
    int64_t quota,
    blink::mojom::UsageBreakdownPtr usage_breakdown) {
  std::move(callback).Run(status, usage, quota);
}

void DidGetUsageAndQuotaStripOverride(
    QuotaManagerImpl::UsageAndQuotaWithBreakdownCallback callback,
    blink::mojom::QuotaStatusCode status,
    int64_t usage,
    int64_t quota,
    bool is_override_enabled,
    blink::mojom::UsageBreakdownPtr usage_breakdown) {
  std::move(callback).Run(status, usage, quota, std::move(usage_breakdown));
}

base::FilePath CreateMediaLicenseBucketPath(const base::FilePath& profile_path,
                                            const BucketLocator& bucket) {
// CHROMIUM_SOURCE_END

// CHROMIUM_SOURCE_SECTION 250
    DeleteSoon();
  }

  void Completed() override {
    DCHECK_CALLED_ON_VALID_SEQUENCE(sequence_checker_);
    weak_factory_.InvalidateWeakPtrs();

    int64_t quota = desired_storage_key_quota_;
    std::optional<int64_t> quota_override_size =
        manager()->GetQuotaOverrideForStorageKey(storage_key_);
    if (quota_override_size) {
      quota = *quota_override_size;
    }

    // For an individual bucket, the quota is the minimum of the requested quota
    // and the StorageKey quota.
    if (bucket_info_ && bucket_info_->quota > 0) {
      quota = std::min(quota, bucket_info_->quota);
    }

    if (is_unlimited_) {
      int64_t temp_pool_free_space =
          available_space_ - settings_.must_remain_available;
      // Constrain the desired quota to something that fits.
      if (quota > temp_pool_free_space) {
        quota = available_space_ + usage_;
      }
    }

    std::move(callback_).Run(usage_ >= 0
                                 ? blink::mojom::QuotaStatusCode::kOk
                                 : blink::mojom::QuotaStatusCode::kUnknown,
                             usage_, quota, quota_override_size.has_value(),
                             std::move(usage_breakdown_));
    if (!is_incognito_ && !is_unlimited_ && !bucket_info_) {
      UMA_HISTOGRAM_MBYTES("Quota.QuotaForOrigin", quota);
      UMA_HISTOGRAM_MBYTES("Quota.UsageByOrigin", usage_);
      if (quota > 0) {
        UMA_HISTOGRAM_PERCENTAGE(
            "Quota.PercentUsedByOrigin",
            std::min(100, static_cast<int>((usage_ * 100) / quota)));
      }
    }
    DeleteSoon();
  }

 private:
  QuotaManagerImpl* manager() const {
// CHROMIUM_SOURCE_END

// CHROMIUM_SOURCE_SECTION 1164
      base::BindOnce(&DidGetUsageAndQuotaStripBreakdown, std::move(callback)));
}

void QuotaManagerImpl::GetUsageAndQuotaWithBreakdown(
    const StorageKey& storage_key,
    UsageAndQuotaWithBreakdownCallback callback) {
  DCHECK_CALLED_ON_VALID_SEQUENCE(sequence_checker_);
  CHECK(callback, base::NotFatalUntil::M148);

  HandleGetUsageAndQuotaRequest(
      storage_key,
      base::BindOnce(&DidGetUsageAndQuotaStripOverride, std::move(callback)));
}

void QuotaManagerImpl::GetUsageAndReportedQuotaWithBreakdown(
    const StorageKey& storage_key,
    UsageAndQuotaWithBreakdownCallback callback) {
  DCHECK_CALLED_ON_VALID_SEQUENCE(sequence_checker_);
  if (report_static_storage_quota_ && !IsStorageUnlimited(storage_key)) {
    HandleGetUsageAndQuotaRequest(
        storage_key,
        base::BindOnce(
            [](base::WeakPtr<QuotaManagerImpl> weak_this,
               UsageAndQuotaWithBreakdownCallback callback,
               blink::mojom::QuotaStatusCode status, int64_t usage,
               int64_t quota, bool is_override_enabled,
               blink::mojom::UsageBreakdownPtr usage_breakdown) {
              if (!weak_this) {
                std::move(callback).Run(blink::mojom::QuotaStatusCode::kUnknown,
                                        0, 0, std::move(usage_breakdown));
                return;
              }
              if (status != blink::mojom::QuotaStatusCode::kOk) {
                std::move(callback).Run(status, 0, 0,
                                        std::move(usage_breakdown));
                return;
              }
              weak_this->GetDiskAvailabilityAndTempPoolSize(base::BindOnce(
                  [](UsageAndQuotaWithBreakdownCallback callback,
                     blink::mojom::QuotaStatusCode status, int64_t usage,
                     bool is_incognito,
                     blink::mojom::UsageBreakdownPtr usage_breakdown,
                     int64_t total_space, int64_t available_space,
                     int64_t temp_pool_size) {
                    int64_t reported_quota = CalculateReportedQuota(
                        total_space, usage, is_incognito);

                    std::move(callback).Run(status, usage, reported_quota,
                                            std::move(usage_breakdown));
                  },
                  std::move(callback), status, usage, weak_this->is_incognito_,
                  std::move(usage_breakdown)));
            },
            weak_factory_.GetWeakPtr(), std::move(callback)));
    return;
  }

  HandleGetUsageAndQuotaRequest(
      storage_key,
      base::BindOnce(&DidGetUsageAndQuotaStripOverride, std::move(callback)));
}

void QuotaManagerImpl::GetUsageAndQuotaForDevtools(
    const StorageKey& storage_key,
// CHROMIUM_SOURCE_END

// CHROMIUM_SOURCE_SECTION 1231
  HandleGetUsageAndQuotaRequest(storage_key, std::move(callback));
}

void QuotaManagerImpl::HandleGetUsageAndQuotaRequest(
    const StorageKey& storage_key,
    UsageAndQuotaWithBreakdownAndOverrideFlagCallback callback) {
  DCHECK_CALLED_ON_VALID_SEQUENCE(sequence_checker_);
  CHECK(callback, base::NotFatalUntil::M148);
  EnsureDatabaseOpened();

  UsageAndQuotaInfoGatherer* helper = new UsageAndQuotaInfoGatherer(
      this, storage_key, is_incognito_, std::move(callback));
  helper->Start();
}

void QuotaManagerImpl::GetUsageAndQuota(const StorageKey& storage_key,
                                        UsageAndQuotaCallback callback) {
  DCHECK_CALLED_ON_VALID_SEQUENCE(sequence_checker_);
  if (IsStorageUnlimited(storage_key)) {
    // TODO(michaeln): This seems like a non-obvious odd behavior, probably for
    // apps/extensions, but it would be good to eliminate this special case.
    std::move(callback).Run(blink::mojom::QuotaStatusCode::kOk, 0, kNoLimit);
    return;
  }

  GetUsageAndQuotaWithBreakdown(
      storage_key,
      base::BindOnce(&DidGetUsageAndQuotaStripBreakdown, std::move(callback)));
}

void QuotaManagerImpl::GetBucketUsageAndQuota(BucketId id,
                                              UsageAndQuotaCallback callback) {
// CHROMIUM_SOURCE_END

// CHROMIUM_SOURCE_SECTION 1267
                         weak_factory_.GetWeakPtr(), std::move(callback)));
}

void QuotaManagerImpl::GetBucketUsageAndReportedQuota(
    BucketId id,
    UsageAndQuotaCallback callback) {
  DCHECK_CALLED_ON_VALID_SEQUENCE(sequence_checker_);

  if (report_static_storage_quota_) {
    GetBucketById(
        id,
        base::BindOnce(
            [](base::WeakPtr<QuotaManagerImpl> weak_this,
               UsageAndQuotaCallback callback,
               QuotaErrorOr<BucketInfo> result) {
              if (!weak_this || !result.has_value()) {
                std::move(callback).Run(blink::mojom::QuotaStatusCode::kUnknown,
                                        0, 0);
                return;
              }

              const BucketInfo& bucket = result.value();
              bool is_storage_unlimited =
                  weak_this->IsStorageUnlimited(bucket.storage_key);

              UsageAndQuotaInfoGatherer* helper = new UsageAndQuotaInfoGatherer(
                  weak_this.get(), bucket, weak_this->is_incognito_,
                  base::BindOnce(
                      [](base::WeakPtr<QuotaManagerImpl> weak_this,
                         UsageAndQuotaCallback callback,
                         const BucketInfo& bucket, bool is_storage_unlimited,
                         blink::mojom::QuotaStatusCode status, int64_t usage,
                         int64_t quota, bool is_override_enabled,
                         blink::mojom::UsageBreakdownPtr usage_breakdown) {

                        if (!weak_this) {
                          std::move(callback).Run(
                              blink::mojom::QuotaStatusCode::kUnknown, 0, 0);
                          return;
                        }

                        // If storage is unlimited, return the real quota value.
                        if (is_storage_unlimited) {
                          std::move(callback).Run(status, usage, quota);
                          return;
                        }

                        // If there was a requested bucket quota, return that
                        // value regardless of whether it was capped at the
                        // StorageKey quota or not.
                        if (bucket.quota > 0) {
                          std::move(callback).Run(status, usage, bucket.quota);
                          return;
                        }

                        if (status != blink::mojom::QuotaStatusCode::kOk) {
                          std::move(callback).Run(status, 0, 0);
                          return;
                        }

                        weak_this->GetDiskAvailabilityAndTempPoolSize(
                            base::BindOnce(
                                [](UsageAndQuotaCallback callback,
                                   blink::mojom::QuotaStatusCode status,
                                   int64_t usage, bool is_incognito,
                                   int64_t total_space, int64_t available_space,
                                   int64_t temp_pool_size) {
                                  int64_t reported_quota =
                                      CalculateReportedQuota(total_space, usage,
                                                             is_incognito);

                                  std::move(callback).Run(status, usage,
                                                          reported_quota);
                                },
                                std::move(callback), status, usage,
                                weak_this->is_incognito_));
                      },
                      weak_this, std::move(callback), bucket,
                      is_storage_unlimited));
              helper->Start();
            },
            weak_factory_.GetWeakPtr(), std::move(callback)));
    return;
  }

  GetBucketUsageAndQuota(id, std::move(callback));
}

void QuotaManagerImpl::GetBucketSpaceRemaining(
    const BucketLocator& bucket,
    base::OnceCallback<void(QuotaErrorOr<int64_t>)> callback) {
  DCHECK_CALLED_ON_VALID_SEQUENCE(sequence_checker_);

  // ConcurrentCallbacks is run once with each space restriction --- the
  // StorageKey usage/quota and the bucket's usage/quota (if it exists). The
  // final value is the more restrictive of the two.
  auto aggregator = base::BindOnce(
      [](base::OnceCallback<void(QuotaErrorOr<int64_t>)> final_space_remaining,
         std::vector<int64_t> space_checks) {
        int64_t space_left =
            *std::min_element(space_checks.begin(), space_checks.end());
        if (space_left == std::numeric_limits<int64_t>::min()) {
          std::move(final_space_remaining)
              .Run(base::unexpected(QuotaError::kUnknownError));
        } else {
          std::move(final_space_remaining).Run(space_left);
        }
      },
      std::move(callback));
  base::ConcurrentCallbacks<int64_t> concurrent;

  // Translates a UsageAndQuota result into a single number for the aggregator.
  auto on_got_usage =
      [](base::OnceCallback<void(int64_t)> report_space_remaining,
         blink::mojom::QuotaStatusCode code, int64_t usage, int64_t quota) {
        // Report the amount of allocated space remaining, or min() for an
        // error, or max() if there's no limit.
        int64_t leftover_space = 0;
        if (code != blink::mojom::QuotaStatusCode::kOk) {
          leftover_space = std::numeric_limits<int64_t>::min();
        } else if (quota == 0) {
          leftover_space = kNoLimit;
        } else {
          leftover_space = quota - usage;
        }
        std::move(report_space_remaining).Run(leftover_space);
      };

  // Check the usage for the whole StorageKey.
  GetUsageAndQuota(bucket.storage_key,
                   base::BindOnce(on_got_usage, concurrent.CreateCallback()));

  // If this is the default bucket, we're done. Otherwise, additionally check
  // the usage of the specific bucket against its quota.
  if (!bucket.is_default) {
    GetBucketUsageAndQuota(
        bucket.id, base::BindOnce(on_got_usage, concurrent.CreateCallback()));
  }
  std::move(concurrent).Done(std::move(aggregator));
}

void QuotaManagerImpl::OnClientWriteFailed(const StorageKey& storage_key) {
  DCHECK_CALLED_ON_VALID_SEQUENCE(sequence_checker_);
// CHROMIUM_SOURCE_END

// CHROMIUM_SOURCE_SECTION 1668
  usage_tracker->GetBucketUsageWithBreakdown(bucket, std::move(callback));
}

bool QuotaManagerImpl::IsStorageUnlimited(const StorageKey& storage_key) const {
  DCHECK_CALLED_ON_VALID_SEQUENCE(sequence_checker_);
  return special_storage_policy_.get() &&
         special_storage_policy_->IsStorageUnlimited(
             storage_key.origin().GetURL());
}

int64_t QuotaManagerImpl::GetQuotaForStorageKey(
    const StorageKey& storage_key,
    const QuotaSettings& settings) const {
  if (IsStorageUnlimited(storage_key)) {
    return kNoLimit;
  }

  if (special_storage_policy_ && special_storage_policy_->IsStorageSessionOnly(
                                     storage_key.origin().GetURL())) {
    return settings.session_only_per_storage_key_quota;
  }

  return settings.per_storage_key_quota;
}

void QuotaManagerImpl::GetBucketsModifiedBetween(base::Time begin,
                                                 base::Time end,
// CHROMIUM_SOURCE_END

// CHROMIUM_SOURCE_SECTION 2375
  }
}

std::optional<int64_t> QuotaManagerImpl::GetQuotaOverrideForStorageKey(
    const StorageKey& storage_key) {
  DCHECK_CALLED_ON_VALID_SEQUENCE(sequence_checker_);
  if (!devtools_overrides_.contains(storage_key)) {
    return std::nullopt;
  }
  return devtools_overrides_[storage_key].quota_size;
}

void QuotaManagerImpl::CorruptDatabaseForTesting(
    base::OnceCallback<void(const base::FilePath&)> corrupter,
// CHROMIUM_SOURCE_END
