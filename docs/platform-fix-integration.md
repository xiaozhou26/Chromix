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
续编使用原生 Ninja `1.13.2` 和独立 `-f` 只读图入口（见下方参数修正），禁止重新 prepare、
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


### Ninja 1.13.2 参数修正（run42626844410）

修复基于 API 核实的 main `a4be5aef98749e3493b903d450fd551b8eb4e264`。
该 run 已完成原 stage8 下载/恢复、源码准入及 before/after source verify；失败日志
第 901 行是 `ninja: error: unknown debug setting 'norebuild'`，并非编译错误。
真实小诊断 artifact `13680446752`（557926 bytes）的 SHA-256 为
`69661ea25636ce5ab49897b46debafe07ea7ffd89641c60be62225fe327fed6b`，已下载核验。

runner 和原 donor 均为 Homebrew 原生 ARM64 Ninja `1.13.2`。核查其上游 tag
`v1.13.2`，commit `3441b633c2fe2c494e958780ba0f4227b1327634` 的
`src/ninja.cc`：`DebugEnable` 不提供 `norebuild`，正确枚举是 `-d list`，
`-d help` 也不支持；`--quiet` 只影响输出，不会禁止 regeneration。
`RebuildManifest` 只查找 `-f` 指定入口的生成 edge。

因此不只删除错误参数：在 work 的 diagnostics 下创建临时私有 `entry.ninja`，
内容仅为 `include build.ninja`。用标准 `-C out/Default -f <private-entry>`
保持原 cwd、原规则和命令；原 `build.ninja`、子图、GN args 不编辑、不复制改写，
私有入口没有生成 edge，因此不会自动走 GN manifest regeneration。
另外使用真实 Ninja 的 `-t targets all` / `-t commands -s` 识别所有现有 Ninja
图和 args.gn 的生成命令，再用 `-n -v` 检查实际 chrome 计划；如果其中包含这些
命令立即拒绝，涵盖普通、implicit、order-only、validation 和已记录 depfile
依赖。单独的 `-t inputs` 不含 validation，不能代替此检查。
未知/多行计划、Ninja warning 或 dyndep 动态图一律 fail closed。预检前后核对
out 全库存，不能创建/重写 log、deps、对象或图；只有通过后才执行真实编译。
临时入口退出时删除，不进入 out，不改变 graph hash 或原四项 Ninja proof。

新增测试使用真实 `1.13.2` 和 C 编译器产出 Mach-O 对象，验证新建对象、已有对象
复用、子图/空格路径、fresh 与 unchanged receipt、各类 regeneration 依赖拒绝及
编译失败传播，不再只 mock `subprocess.run`。固定原始 artifact/manifest、226
patch/source-final、SDK 内容、core/tooling、GN/PGO 与 runtime gate 合同均保留。
完整 Chromium compile 与原生 runtime 验收仍须父任务合入 main 后调度验证。

本次专用测试为 21 passed、38 subtests passed；与 mac156 donor、Linux156
checkpoint、旧 Mac checkpoint、Ninja 合同组合复跑为 126 passed、17 skipped、
264 subtests passed（`/tmp/chromix-mac156-ninja-fix-regression.log`）。使用
`COPYFILE_DISABLE=1 TMPDIR=/private/tmp /tmp/chromix-156-venv/bin/python`。
AST 对比确认只有原 `compile_tree` 调用路径改变，所有原身份常量以及 source_state、
SDK/core/recipe 准入函数保持一致。未 push、未 dispatch。


### 真实 GN156 phony/stamp 图修正（run42632751681）

基线 main 已通过 API 核实为 `4d75297d480b22dcb09da08ecbab6af693ad6833`，
本次使用独立 `/tmp/chromix-mac156-gn-graph-fix` worktree，不覆盖此前工作区。
新 run 小诊断 artifact `13683691524`，557936 bytes，SHA-256
`ea20ec99843f2992cc1dcc935b205b2f3aaaf32b1379281966940e495de6c962`
已经下载核验；其中没有保存 Ninja probe stdout/stderr，不能靠它推断实际图。

这次完整下载并校验原 donor artifact `13675184448` 的 9643478210 bytes ZIP，
SHA-256 仍为 `b943ead7eaf8ba1f3b458763fdd42c381f5b9e440ad352448283971181d6da56`。
从其中单卷提取了 13,463 个未经改写的 Ninja 图文件及 GN/stamp/args/log/deps。
全部图 hash `3f8de07349384af93596270af54e4cd3fa707179ddd55265c8b6393495226fd7`
和三项原 Ninja 文件 hash 均与新 run 的 source 准入收据一致。完整 archive 保留于
`/tmp/chromix-mac156-exact-donor-13675184448.zip`，提取的只读图在
`/tmp/chromix-mac156-donor-graph`。

真实结构不是“没有 GN 生成器”：`build.ninja` 的生产边为
`phony build.ninja.stamp`，`build.ninja.stamp` 的生产边为 `gn`，实际命令是
`./gn --root=../.. -q --fail-on-unused-args --regeneration gen .`；
`args.gn` 不在 producer target inventory 中，是预先存在的配置输入。
对完整 donor 图实际执行 `ninja -t commands -s build.ninja` 得到空 stdout、空 stderr，
而传递 `commands build.ninja` 得到上述 GN 命令。完整 inventory 有 311,011 targets。
这解释了上一次“必须有直接命令”的错误假设，而不是 source verify 失败。

helper 现在从所有受保护 `.ninja`、`args.gn`、`build.ninja.stamp`、`build.ninja.d`
开始，以 `targets all` 识别 producer roots，再用只读 `-n -t query` 遍历 phony、
普通/implicit/order-only inputs 和 validations，直到真实 executable producers。
`-n` 防止 query 的 RUN_AFTER_LOGS 路径写入或 recompact 原日志。
每个非 phony producer 必须能返回非空单行命令；只有 inventory 证明无 producer，
或完整闭包全是 leaf/phony 时，空命令集才有合法图证明。未知格式、缺失节点、
rule 与 inventory 不一致、warning 和动态图继续拒绝；不再把空 stdout 当作证明。
实际 dry-run 仍阻断 GN 及受保护文件的所有间接生成命令，私有 `-f` 入口继续禁止
自动 regeneration，源码/GN/PGO/已有 out 对象不变。

每次 probe 的参数、返回码、stdout/stderr 和 `regeneration-proof.json` 现在写到
现有 fingerprint-diagnostics 上传目录；失败也保留诊断，临时入口仍清除。
新 proof 已在完整原 donor 的 13,463 个图上通过，确认唯一 producer 为 stamp 的
`gn` 命令，整个 out 库存前后相同。

测试夹具 `tools/tests/fixtures/macos156_donor_gn_graph.json.gz` 保存真实 donor
header、probe、完整图 proof 摘要以及 donor `src/tools/gn/src/gn/ninja_build_writer.cc`
源码。另用从同一 archive 提取的 GN 二进制（SHA-256
`a3d593a9aa898634f8d6b04a6a929af2e65ccb876a5f4b4185366db24eff2de1`，
版本 `2578 (510ec7992c30)`）实际生成最小 C 构建图，保存输入和原始生成文件。
真实 Ninja 测试覆盖 phony 无直接命令、无 producer、phony-only、间接 stamp/validation
危险分支、未知图/空 executable command/warning 拒绝及已编译对象复用。
无 producer 变体明确由真实生成图移除生成边构造，不冒称 donor 本身没有生成边。

完整 Chromium 源码和对象未全量解包；全 donor 图生产链证明与真实 GN 小构建已验证，
完整 chrome dry-run/compile、SDK 原生 runtime 仍需父任务最终调度，不能视为已通过。
本次不修改 workflow、旧恢复工具、source/SDK/core 验证链或 runtime gate，不 push/dispatch。

最终针对性组合回归：133 passed、17 skipped、269 subtests passed，日志
`/tmp/chromix-mac156-gn-graph-final-tests.log`。最终 helper 在完整 donor 图再次验证
生产链 proof 通过，out 全库存未变；结果保存在
`/tmp/chromix-mac156-actual-probes/proof.json`。Python 编译、`git diff --check`
通过，AST 比对确认仅原 `frozen_ninja` 函数发生变化，其余原验证函数及身份常量
保持一致。


### Ninja dry-run 原 out 写入隔离（run42640658112）

本次基线通过 API 核查为 `59e69b31ac737e981d4aef61b17a31e16f79b094`，独立
worktree `/tmp/chromix-mac156-preflight-state-fix`。实际诊断 artifact `13686994871`
为 3732475 bytes，SHA-256
`3d68e1ca43e916cd599bbec73825c69af09faf9b01c814b60ff433dcd0820268`，已下载校验。
四次 probe 返回码均为 0，stderr 全空；云端 before/after graph、build.ninja、
.ninja_log、.ninja_deps hash 全部相同。此前未保存全库存差异，不能声称已知云端
每个变化路径，也不能据此猜测日志重整。

已将认证原 donor ZIP 全量解包至 `/tmp/chromix-mac156-full-donor`，用完整图、
30,266,486-byte .ninja_log、505,783,580-byte .ninja_deps、真实源码和对象，
对云端真实计划里的 `obj/ui/views/debug/libviews_debug.a` 执行 Ninja 1.13.2
`-n -v`：返回 0、stderr 为空，94 个目录 mtime/ctime 改变，文件集合/模式/大小/
inode 和三项 Ninja 文件 bytes hash 均不变。精确 before/after 数据保存在
`/tmp/chromix-mac156-preflight-reproduction/target-difference.json`，并收入测试夹具。
这提供实际可复现的失败机制，不将它冒称为云端未记录的完整差异清单。

官方 Ninja v1.13.2 `src/build.cc` 的 `Builder::StartEdge` 在 dry_run 下仍执行
MakeDirs 和 rspfile WriteFile；FinishCommand 随后删除 rspfile，导致目录 metadata
变化，甚至可能覆盖并删除已有 rspfile。OpenBuildLog/OpenDepsLog 的写入和重整
则由 `!dry_run` 阻止。`keepdepfile`/`keeprsp` 不会阻止上述写入，不能作为修复。
原 out 的严格不变断言保持，不恢复/伪造 timestamps，不删除原 logs，也不跳过预检。

现在把完整 src（包含全部 graph、log/deps、源码、已有对象、symlink、mtime）复制为
work 下与 src 同层的私有 clone。常规文件使用 macOS clonefile 独立 copy-on-write
inode；目录和链接显式复制，核对完整 inventory、mode/size/纳秒 mtime/link，禁止
hardlink、稀疏占位和“只有图而所有输入缺失”的检查。相同目录层级保留 `../../`
源码及 `../../../tooling`、爬到文件系统根的外部输入语义。clone 失败直接拒绝。

所有 Ninja probes 在 clone 上执行，macOS sandbox-exec 只允许写入该 clone，
即使图输出经绝对路径或 symlink 指向原 src/out/外部目录也会被阻断。既有 GN
生产链证明、dry-run regeneration 拒绝和 warning/未知图拒绝全部保留。
预检退出记录 original_changed 与 scratch_changed 的逐路径六字段 before/after
差异；original 必须完全为空。只在批准后回到原图和原 cwd 执行真实编译，不发布
scratch 对象或日志；clone 无论成功失败都清理。

新真实 Ninja 测试复用原 donor alink/rsp 规则，覆盖目录创建、已有 rsp bytes 被
覆盖删除的复现与隔离、relative tooling 输入、sandbox 拒绝外部/原路径写入，
以及 rsp C 编译成功和已有对象复用；此前危险 GN/phony/validation guards 继续运行。
完整 chrome 本地 dry-run 因缺少 donor Xcode_26.6 的绝对 gperf 输入而 fail closed，
未伪造 SDK 或 missing 文件；因此不声称本机完成完整 chrome/原生验收。

最终在完整 952,203-entry donor src 的逐文件 clonefile + sandbox 上实测同一真实
archive 子目标：dry-run 返回 0、stderr 空，scratch 中 95 项目录 metadata 变化，
**原 src（包含 out）全库存 0 差异**。完整报告在
`/tmp/chromix-mac156-preflight-reproduction/isolated-files/preflight-state-difference.json`，
clone 记录和精确差异也已收入 `macos156_preflight_state.json.gz`。其中 94 项是 out
子目录，另 1 项是 out/Default 根目录；都仅发生于 scratch，不回写原树。
旧的目录级 `cp -cRp` 大树尝试未通过，最终实现没有保留该路径，使用仅常规文件
clonefile 与显式目录/symlink 复制，完整实例已通过。
针对性组合回归为 136 passed、17 skipped、269 subtests passed；专用测试 31 项
通过。原函数 AST 对比仅 frozen_ninja 改变，既有 regeneration_proof、source_state、
native_state、compile_tree 身份检查及所有常量保持不变。未 push、未 dispatch。

对照原 donor 与隔离 clone 的同目标 dry-run，去除唯一不同的 Entering directory
行后，2,275 条命令逐字节完全一致，命令次序也相同；已把数量与 hash 收入夹具。
这验证 clone 未因相对路径错位或遗漏已有对象而把全部输入误判为 missing。
