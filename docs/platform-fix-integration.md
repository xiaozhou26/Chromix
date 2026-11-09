# 跨平台修复整合

本次将已完成的平台源码修复与检查点恢复工具统一到 main，保留已有 SDK、网站和 Docker 发布文件。源码版本与已发布二进制的下载版本分别管理。

## 已整合范围

- Linux155：TypeScript/Node 启动、声明兼容、真实 profile controller 链接、不透明 PNG 导出一致性、固定检查点迁移。
- Windows155：MIDL、metagen LLVM 绑定、AMD64 宿主与 PE 架构验证、PGO 参数保留、PNG、启动时区同步及 WebString API 修正、三代检查点恢复工具。
- HTTP/2：连接关闭和证书生命周期的辅助诊断，保留既有复用、GOAWAY、恢复验收断言。
- macOS154：Helper 完整性、正常退出补丁、固定文件树恢复及退出诊断。

源码补丁数为 Linux155 226、Windows155 231、macOS154 225。根补丁224份保持不变。

## 历史恢复约束

历史恢复工作流绑定 donor、目标提交、工具字节、源码收据及补丁身份。代码统一到 main 后，仍须使用各文档指定的历史恢复分支和输入；main 的新增修复不自动成为旧检查点允许的迁移目标。测试使用固定历史提交重建夹具，继续拒绝未审核的工具、GN 或补丁变化。

## 验证边界

原生成功的 Windows x64 与 ARM64 155.0.8059.39 包已独立发布；其来源提交和补丁数分别记录，不能将一方验证结果套用到另一方。源码合并不代表 macOS 的 HEVC/MIME/RTC 等剩余问题已解决，也不证明间歇 HTTP/2 问题已根治。

本机 macOS 不提供 Linux `renameat2`，相关 Linux 原子发布测试需在 Linux 上执行。新的上游缓存和版本适配在独立分支进行，逐平台完成完整云端编译及原生验收后再更新发布资产。

## macOS156 ARM64 固定 stage8 只读续编

2026-11-09 已通过 GitHub API 核查 `origin/main` 为
`e52742b38cf832538f85ac2e423fd315f9486d21`。专用入口
`.github/workflows/build-mac156-arm64-stage8-resume.yml` 仅允许手动设置
`compile_jobs`，其余来源、`fast`、mandatory upstream cache 和 `macos-26`
原生 ARM64 runner 固定。本地提交不自动 push/dispatch，由父任务合入 main 后调度。

固定来源（不是旧 mac155 selector/身份，也不接受任意 source SHA）：

| 字段 | 固定值 |
| --- | --- |
| run / attempt / stage | `42455180503` / `1` / `8` |
| job | `129075816793` |
| donor SHA | `2e2a85fdeb42f37a7528a7d1ced247deab337452` |
| donor tree | `45453c3968522416c696ea431f93db835890d7d2` |
| artifact | `13675184448`, `chromix-mac-arm64-tree-s8-attempt-1-part1` |
| artifact ZIP bytes | `9643478210` |
| artifact SHA-256 | `b943ead7eaf8ba1f3b458763fdd42c381f5b9e440ad352448283971181d6da56` |
| packed volume bytes | `9643477952`, 单卷 `tree.tar.zst.001` |
| job log bytes / SHA-256 | `635568` / `482d3374465856ea5db15ecdd1506fd4eb7e8259203a6dc0d1bb96157e36a29d` |
| diagnostics artifact | `13675084494`, `533680` bytes |
| diagnostics SHA-256 | `aeaceea443bf570a0c1fcddcaeb007ec2609d299a4885bba85b8d942e38e06f6` |

真实完整 job log 证明 `stage 8 reached max-stages 8 without finishing`，不是
compiler error。四个 upload step API 均为 completed/success，但 part2–4 日志明确
`No artifacts will be uploaded`；只接纳 part1，拒绝缺卷、额外卷、旧 stage/attempt、
过期产物和不匹配的 ZIP digest/size。下载器必须完成 digest 校验和原子发布，准入收据
保存 manifest、单卷 digest/size、原 run/job/attempt 和当前 continuation run。

`tools/tests/fixtures/macos156_arm64_stage8_receipts.json.gz` 来自上述实际小型
诊断 ZIP 和完整 GitHub job log，不是推测的 source-final。源码为
`156.0.8078.79`、精确 226 patches；source-final SHA-256 为
`afebdcf64f7a79524d20aa942feb0e826fa427a35f1953e0d960323096afc421`，
GN args SHA-256 为 `aca4229e636c0cfff25e7fe0e8bfae588ce82fef5a0a8a7cff4b8333aa59e750`。
prepare key 固定为
`macos|arm64|156.0.8078.79|28a211ab4ab6b83d919966a4f58a6df23cf4fb29|31f01756a43d55b2c4211e73ca329bfab4ebfcb2|0c828be269b7a653ffcec6ff9392346a72f6f96eb9716792c270bd9240b17914`。

专用 helper 逐字节核对 macOS recipe、选中 patch/manifest、上游缓存标记、真实
source-final 和准备收据，并通过 scratch reverse/forward 复验真实源码。
当前 main 的 Linux/Windows 修复保留；共享 `prepare_restored_build.py` 只接纳
两个经过审核的完整文件 hash，并验证去除唯一 Linux TypeScript 函数后 AST 相同，
不以“donor SHA 可信”替代 macOS recipe 等同证明。其他恢复 helper 合同不变。

准入阶段只写新的 diagnostics/continuation receipt，不修改源码、准备标记、
GN args、PGO 或 out 文件；对 out 库存和 Ninja 状态作前后检查。
续编只运行原生 Ninja `1.13.2 -d norebuild ... chrome`，禁止重新 prepare、
GN/bootstrap、工具修复、清理输出和 cold fallback。当前有效 PGO 仍是 donor
`chrome_pgo_phase = 0`，不能拿上游 original_args 中被覆盖的 phase2 代替。
每 stage 开始重新检查原生工具、工具链/生成器 hash、SDK 内容/环境及 Rust loader；
不兼容时 fail closed，不重新准备。

续编使用独立产物前缀 `chromix-mac156-arm64-stage8-resume`；local stages 1–8
对应 logical stages 9–16。每个后续 stage 必须匹配同 run 前一 stage 的 handoff
状态，下载限定 producer attempt；GN 图固定，Ninja log/deps 只允许编译推进。
到第八个新增 stage 未完成仍失败并保留最新快照，不报告成功。最终 ZIP 文件名仍为
`chromix-mac-arm64.zip`，上传 artifact 名与快照使用独立命名空间。
编译完成后的 packaging、native smoke、HEVC/CDP 和 fingerprint acceptance gate
继续走既有路径，不放宽验收。

本地验证使用 `COPYFILE_DISABLE=1 TMPDIR=/private/tmp` 和
`/tmp/chromix-156-venv/bin/python`。真实 metadata 与 diagnostics 已在线验证；
9.64 GB 完整 snapshot 的下载/解包、真实 native 编译及 runtime 验收须由 GitHub
执行，不能将夹具单元测试视为完整 donor 源码或成品验证。

本地扩展回归中，旧 `test_cross_platform_build.py`、
`test_prepare_restored_build.py`、`test_macos_runtime.py` 的 66 个失败条目已在
未修改的 `e52742b` 对照 checkout 完整复现，失败集合相同；包括固定 mac154 版本
断言和在 macOS 上运行 Linux `renameat2` 原子替换测试。本次不改这些既有合同或
扩大到无关修复。新专用 helper 的 15 项测试通过；新旧恢复合同组合复跑为
30 passed、1 skipped、68 subtests passed。YAML 解析、161 个 workflow run 脚本
的 bash 语法检查及 `git diff --check` 通过。
最后针对 POSIX snapshot/download/restore、mac156 donor、Linux156 TS migration、
旧 Mac 恢复、workflow、Ninja 和 restored-tools 的组合回归为
532 passed、33 skipped、1406 subtests passed。测试日志保存在本机
`/tmp/chromix-mac156-arm64-stage8-final-tests.log`；pytest 已完成通过，外层 zsh
随后使用只读变量名 `status` 导致包装命令退出 1，不属于测试失败。
