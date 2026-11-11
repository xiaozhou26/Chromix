# Chromium 156.0.8078.79 六平台构建

源码构建目标统一为 `156.0.8078.79`，core commit 为 `28a211ab4ab6b83d919966a4f58a6df23cf4fb29`。平台版本与来源分别固定，版本升级不自动变更 SDK 下载默认值或已发布资产。

## 固定来源

| 平台 | 平台提交 | 构建方式 |
|---|---|---|
| Linux x64 / ARM64 | `6f0aa178c2394fc0a177edc0fd64e41f402a0418` | 恢复 run `41095740465` 的精确双架构缓存 |
| macOS Intel / Apple Silicon | `31f01756a43d55b2c4211e73ca329bfab4ebfcb2` | 恢复 run `41595328070` attempt 3 的精确双架构缓存 |
| Windows x64 / ARM64 | `8492884eb30fdd9417ea7816aa92def805c97c74` | 用户确认首次从精确版本源码构建；后续阶段保存及恢复文件树 |

Linux artifact 为 `13026649061` / `13033900099`，macOS 为 `13284623799` / `13269271368`。名称、大小、SHA-256、到期时间及 gitlink 身份见 `build/upstream-cache.json`。

Windows 原156上游缓存已不可下载，manifest 明确记录 `available=false`。不得将155或157缓存当作156使用，缓存开启时必须拒绝下载；本轮 Windows 使用显式 `use_upstream_cache=false` 首次构建，用户已确认该成本。其余四个平台继续使用缓存，恢复失败停止任务。

## 补丁和工具

- 原224基础补丁字节保留；156使用23个精确覆盖补丁，绑定真实源码及审查清单。
- Linux / macOS 各226个有效补丁，Windows232个。包含真实控制器实现链接和不透明 PNG 一致性修复；Windows另保留时区同步安装修复。
- `build/patch-selection-156.json` 与固定摘要绑定；未知版本、错配 gitlink、篡改补丁及额外文件均拒绝。
- Linux156恢复 TypeScript JavaScript 编译器路径，严格限定系统 tsc/esbuild外链处理并重新安装固定宿主工具。
- macOS156要求 Xcode / SDK 26.5或更新的兼容版本，并保持 SDKROOT 一致及同架构原生构建。
- Windows156要求 SDK 10.0.28000.0，metagen绑定精确V8源码模块及LLVM23.1.0、AMD64宿主Python和DLL身份。PGO参数保留。

## 构建与发布

从 main 显式启动六个平台的分阶段工作流。首次编译及每阶段输出保存文件树，失败从最新通过校验且成功上传的检查点恢复；历史155迁移工具仍只接受其原有固定身份。

通过补丁顺序应用和反向/正向回放只证明结构兼容，不能替代完整编译和原生浏览器验收。所有原有验收断言、沙箱、包架构及运行检查继续执行。此前macOS HEVC录制和Intel CDP超时问题保持未关闭，需156实际验收确认，不能视为版本升级自动修复。

平台独立通过完整原生门禁后方可更新对应发布资产；构建或验收失败的平台保留原发布版本。

## Intel 最新 stage3 超时检查点恢复

`build-mac156-x64-stage3-resume.yml` 仅允许从 `main` 手动启动，固定恢复 run
`42735558594`、job `129753489228`、attempt `1`、失败 stage `3`，生产提交为
`f6da88f521078f8577c65b4b8d5426ab36a4606d`。唯一树产物是
`13757361205`（`chromix-mac156-x64-stage2-resume-tree-s3-attempt-1-part1`），ZIP
大小 `8738426126`，SHA-256
`a9a261d11c512e4529e9a1a159c55295de9bbf3c3c6601c64751560aaf87d759`；解包单卷为
`8738425868` 字节。下载器必须实际验证 ZIP 大小、摘要、CRC 和单卷完整发布，不接受
stage2 产物、缺卷、旧 attempt 或 cold fallback。

生产日志显示 SDK 完整内容预检成功，随后只读 continuation gate 的
`xcrun --sdk macosx --show-sdk-build-version` 在 30 秒后超时，尚未写 stage3
before 收据或执行 Ninja。这证明的是系统命令超时，不是 compiler error，也不是 SDK
值不符。恢复策略是新 runner 上重新执行同一原生校验，不延长 timeout、不添加 retry、
不伪造 xcrun 输出或跳过 SDK 比较；新 runner 仍超时或值不符时必须失败，不能视作通过。
这与浏览器原生验收重试无关，HEVC/CDP 门禁及 ARM 运行合同不变。

独立 stage3 helper 固定失败运行/步骤/日志及原 stage8、stage2 admission、stage1/2
before/after 收据字节；原 stage2 helper 的历史身份保持不变，仅复用其 admission、
continuation 和冻结图 Ninja 执行逻辑。恢复的是最新 stage3 失败树，并要求 Ninja
state 等于已完成的 stage2 handoff（逻辑 stage11），新八段对应逻辑 stage12..19。
prepare key、226 个补丁、source identity、GN/PGO、完整 SDK 内容、工具链、生成器、
原生 Ninja 和后续验收全部维持原比较条件；不得重跑 GN 或全量 preparation。

本地定向测试应使用与生产者一致的 Python 3.13（现有 recipe 收据包含 Python AST
序列化摘要），运行 `tools.tests.test_macos156_x64_stage3_resume` 以及原 stage2、
stage8、ARM continuation 回归。测试不替代 GitHub Intel 原生编译/验收；合入后可立即
单独启动本工作流，无需等待 ARM HEVC 调查。
