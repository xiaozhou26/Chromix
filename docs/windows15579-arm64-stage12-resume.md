# Windows155.79 ARM64 stage12 固定树续编

## 本地交付与边界

- 分支：`build/windows15579-arm64-stage12-resume`。
- 独立 worktree：`/tmp/chromix-win15579-arm64-stage12-resume`。
- 基线：本地 `build/windows15579-donors` 的 `c89c94f4bbbbac853093c1956083b678f0a3ed9b`。
- 只读 GitHub API 核验远程 source `8454c240f86343ec283bdd10d1b5d778f6624e48` 的 tree 为 `b699059af377aaa38d8c01177130ca1e93debedd`，与本地基线完全相同。两者 commit SHA 不同，不能互相冒充。
- 已确认本地基线包含主分支整合祖先 `743d17a`；未覆盖其他 worktree。
- 仅新增 workflow、验证工具、控制器 admission 分支、测试和本报告；patches、版本 pins、上游 manifest、GN 参数文件和 prepare 实现保持不变。
- 未 push、未 dispatch、未发布；由父审阅者检查本地提交后通过 API 发布。

## 独立 donor 审计

固定 producer：run `39104889810`、stage12、attempt1、job `118608015312`，workflow `build-win-arm64-github`，branch `build/windows15579-donors`，source SHA 如上。

只读 API 返回 run/job 均为 completed/failure。`Run stage 12` 失败，`Ensure build tree snapshot` 和四个 upload slots 均成功；实际完整树是两个分片，而不是四个非空 artifacts。完整 job log 证明 Ninja 被时间预算终止，随后报 `build did not finish within 12 stages`，不是 compiler error。日志中未找到 `FAILED:`、`fatal error C...` 或 `error C####`。日志 SHA-256 为 `530da3b8a27f0f2c50773ac660be87352280f40882d7dad17c498976d2d265ce`，255543 bytes；验证器同时 pin 日志摘要和 upload finalization 记录。

| Artifact | ID | Bytes | SHA-256 |
|---|---:|---:|---|
| `win-arm64-tree-s12-attempt-1-part1` | 12281582560 | 9663676664 | `f990b005ad29b65a1d97d6616e2b61d319244bc2ea4ec7a043b71e056a9e4bb9` |
| `win-arm64-tree-s12-attempt-1-part2` | 12281343067 | 2948094979 | `b699f57b13dd7f5a099e5d33f9f695be765ffd616286ed5b4953fab0b1b54a27` |
| `upstream-reuse-win-arm64-build-12-attempt-1` | 12280535203 | 36169 | `3cd359a5ccfb16da62ac1ef7705873a9e491c9c16431772c51cf41d0e0ffe203` |
| `fingerprint-win-arm64-build-12-attempt-1` | 12282015254 | 263662 | `feb3ddc33d1c770422a4124d30474103d8ae8a132c883f0915cb9bc6c5fc6d90` |

核验时全部未过期。树分片到期时间分别为 `2026-10-23T16:29:25Z`、`2026-10-23T16:30:47Z`。过期必须失败，不转用 latest、其他 attempt、早期 stage 或冷构建。

两个小型 evidence ZIP 已实际下载并逐字节核对上述大小和 SHA-256；大树 ZIP 的元数据和 producer finalization 已核验，本地未下载约 12.6 GB 的树。运行时复用 `download_windows_snapshot.py` 校验实际 ZIP digest/size、卷序列，再检查 7z listing、安全路径和必需 incremental state，之后 `7z t` 并解压。

本地原始审计资料保留于 `/tmp/chromix-win15579-arm64-stage12-audit/`；确定性压缩 fixture 保存真实 API、prepare/source receipts 和完整 job log。

## 原始 source / prepare 身份

- Chromium：`155.0.8059.79`，Windows ARM64，231 patches。
- Core：`9f145d8815e6e50cc4a2537fcac40966d911e027`。
- Windows overlay：`a1e3123c8d543b5c9a1eecfaf0f06cf242c0db37`。
- 最初上游 run `38678494961`，artifact `12107338297`，不重新 fetch。
- 原 prepare-key：`155.0.8059.79|9f145d8815e6e50cc4a2537fcac40966d911e027|a1e3123c8d543b5c9a1eecfaf0f06cf242c0db37|bfea4512b09671df8ccce0a523395da545a7fda3eb2d0faf1f3f95ddc83a4ce6`。
- Windows `.tq` lite payload 使用 CRLF；macOS checkout 是 LF，本地直接计算 key 会得到 `67b09665...`。测试重现该差异；runtime 只承认实际 donor 原 key，绝不改写 source-ready/patch-selection。
- Source identity SHA-256：`0c11c2b9ffdfaf8465dba86861838fb991a33c4800aff046a612f7f1c2bdfede`。
- Source outputs SHA-256：`11b1388812bd33a8229259e0104cb4528882a2936b35bf198a31285a55a475cb`。
- 原 `source-5a11481bd060472ea21648181aeb0e9f.json`：`2c815c958e4baa3c5e9c3f928b97a5563b91cf78fc871d503875559adbe94ea4`。
- Prepare receipt：`36ffa453991505c56f7a3d5a21304578c95bae3f60d07726b8b3ef91fe2a61c9`，ready_for_gn=true；first_finish、tool_swap_invalidations、toolchain_invalidated_outputs 均为 0，compiled removed_outputs=0。
- Upstream receipt 使用 reuse collector 相同的 canonical normalization（去掉 manifest.path）后摘要为 `66889e702bbab30c7fac90026875ba1358fd0bf3c0d0011a887bccfa38bcd436`；不是把它当原始文件字节摘要。

Admission 每阶段检查真实 target HEAD、只允许控制器/测试/报告差异、donor tree、原 key、source receipts、231 补丁输出、只读反向/正向验证、upstream identity、原 GN 合并配置与 PGO2、toolchain/generator hashes、host SDK 配置。写入独立 continuation receipt，真实 target SHA 与原 donor SHA 分开记录。原 markers 和 prepare receipts 不变。

## 控制器行为

入口 `.github/workflows/build-win15579-arm64-stage12-resume.yml` 只支持手动调度，唯一输入 `compile_jobs`。基于现有 ARM workflow 模板生成，不替换普通工作流。

- 十二个 jobs 对应逻辑 stage13–24；第一个只从固定 stage12 attempt1 下载。后续只取本 run 前一 job 输出的 `snapshot_attempt`，不使用当前 attempt 猜测，不按 latest 搜索。
- `ci-stage.ps1 -ResumeStage12DonorRepo` 仅允许 native ARM64 .79、FromArtifact、required upstream receipt、13..24、MaxStages=24；禁止冷回退、migration 和 ValidateOnly。
- 跳过 `prepare-ungoogled.ps1`、`prepare_restored_build --finish`、GN merge、显式 `gn gen`、metagen `--configure`、bindgen rebuild 和 source 修改。仅检查兼容 Ninja，安装不在 snapshot 内的 host Python clang bindings，随后 dry-run 和增量 Ninja。
- Admission 对源文件及 Ninja 元数据作前后 hash/stat 检查；不会删除或失效 out 对象。编译产生的新增/更新对象属于正常增量进度。
- 使用原 Job Object 跟踪及清理门禁；`snapshot_safe=false` 起步，严格 admission 通过后才允许保存，执行期间仍由 process-tree cleanup 控制。
- 预算耗尽/编译失败均保留可用最新完整树；最终编译完成也保存树，避免 native 验收失败只剩 bundle。树和诊断保留 7 天。未经安全确认的树不被标成有效 checkpoint。
- native Windows11 ARM job 原样复用，包含 ZIP/SHA256/PE architecture/version/headless exit 及完整 fingerprint acceptance；native diagnostics 总是上传。编译完成不能替代 native acceptance。

如后续编译或验收失败，应审计该次 run 的最新有效树并建立新的固定 continuation，而不是重新从此原始 stage12 开始。此入口故意不提供跨 run latest 自动选择。

## 本地验证与未验证项

使用 `/tmp/chromix-spotlight-venv/bin/python`、`/tmp/chromix-new-pwsh/pwsh`，测试临时目录设为 `/private/tmp`，避免 macOS `/var` 符号链接触发安全路径拒绝。

新增针对性测试覆盖真实 API/receipt/log fixtures、错误 run/job/attempt/digest/size/expiry、缺失/多余分片、unsafe archive paths、source identity 与旧 .39/230-patch 混用、Windows CRLF 原 key、真实 target SHA、初次/后续 admission、十二阶段 attempt 链、失败树保留及 native 验收不变。PowerShell parser 检查 stage 脚本及生成 workflow 所有 PowerShell 片段。

完整针对性回归包括 `test_windows15579_arm64_stage12_resume`、`test_windows_arm64_workflow`、`test_windows_arm64_build`、`test_ci_stage`、`test_windows_cleanup_gate`、`test_download_windows_snapshot`、`test_windows_metagen_bindings`、`test_verify_windows_bundle`、`test_windows_upstream_cache`。历史 slice harness 仅隔离新增 resume 分支；生产 legacy prepare 路径未改动。

最终本地结果：新增恢复测试 19/19 通过；九组完整回归共 312 项，295 通过、17 跳过、0 失败。跳过项需要 Windows Job Object API 或本机缺少的 pinned ungoogled verification tooling，不计作原生验收通过。两套 workflow generator `--check`、全部 PowerShell 片段解析、`git diff --check` 和 `tools/check_patches.py` 均通过。日志为 `/tmp/chromix-win15579-arm64-stage12-audit/regression-tests.log` 与 `targeted-tests.log`。

本地未执行真实 Windows 源码解压 admission、续编/链接或 Windows11 ARM runtime acceptance；这些必须在父审阅者发布后的 CI 中验证。原 reuse report 的 `retention_proven=false`/`incomplete` 保留原结论，不能从 metadata 或源验证推断实际 cache hit、完整对象保留或 runtime 成功。
