# Chromium 157.0.8103.78 六平台构建

目标版本统一为 `157.0.8103.78`，精确 ungoogled core 为 `8c886dffc6ade39d8f7df5d64ace5a8824b455e1`。源码更新直接提交 main；发布资产、SDK默认下载和Docker标签分别管理，不提前指向尚不存在的包。

## 来源

| 平台 | 平台配置来源 | 首次构建方式 |
|---|---|---|
| Windows x64 | 官方 `157.0.8103.78-1.1`，`20e9a348fc616bd73a366fbd5d8881613d6c5cb0` | 原源树缓存已删除，按用户授权首次源码构建 |
| Windows ARM64 | 同上 | 精确 artifact `14178413147`；过期/下载/身份失败即停止 |
| macOS x64 / ARM64 | 官方 `157.0.8103.78-1.1`，`84768e74ece113d9e68e5d774808afe6c3a8fbe6` | run `43804136377` attempt1，artifact `14228304612` / `14214802687` |
| Linux x64 / ARM64 | 固定 portablelinux156基底 `6f0aa178c2394fc0a177edc0fd64e41f402a0418` + 明确的157 core覆盖和九项已重基平台补丁 | 用户授权首次源码构建；当前没有官方portablelinux157 tag或缓存 |

Linux是明确记录的下游组合来源，不把平台原gitlink改写成官方157身份。其构建源码仍严格为157.0.8103.78；原platform/gitlink与effective core均记录并校验。禁止把156缓存当作157输入。

缓存大小、SHA256、run/job/attempt及到期时间见 `build/upstream-cache.json` 和对应验证工具。Windows ARM64缓存到期为2026-11-19 20:14:29 UTC，若不可用需明确处置，不能自动退回冷构建。

## 补丁与验证

原224基础补丁及所有历史版本清单保持字节不变。157以精确覆盖清单重基：Windows232、macOS228、Linux226个Chromix补丁。Linux另有九项明确绑定的平台覆盖补丁。对精确core/platform源文件执行顺序应用和反向/正向回放，实际编译及运行结果另行确认。

复用已有PNG、控制器链接、时区修复；macOS包含空ANGLE参数与HEVC录制MIME修复。源码重基不等于所有历史Canvas/CDP问题已被运行验证解决。

## 发布规则

Windows和Linux保留现有原生验收门槛。macOS按用户要求编译成功即可发布：包安全布局/CRC、目标架构、版本、SHA256以及源码收据和真实源码哈希必须正确；原生启动和指纹诊断继续运行，但失败、超时或incomplete不会阻塞合格编译包上传，原始状态及错误如实记录。

macOS已观测到的浏览器身份不匹配仍阻塞；仅未取得运行身份不会伪装为不匹配或通过。最终bundle与源码收据优先上传，辅助快照或诊断上传故障不吞掉已验证编译包；未完成编译的阶段仍必须完整保存有效交接快照。

所有平台在失败时保留能通过完整性检查的最新文件树，下一轮从最新有效检查点继续。构建控制台显示阶段成功仅表示该阶段完成，并不表示整个平台已编译或发布。六平台全部完成后再统一 release；单平台续编不提前发布。

## macOS Intel stage6 精确续编

`build-mac157-x64-stage6-resume.yml` 是独立入口，固定 `macos-26-intel`、fast、必须恢复缓存、compile-release；不改变 ARM stage5 入口。新 stage 1..8 对应逻辑 7..14，不重新获取 upstream 或 fresh prepare，不重新生成 GN。

- Producer：run `44639554770`、attempt `1`、成功 stage6 job `136513975353`，源码 `35e2db1b0687a4525017f5ec8fd3820823aef8ff`，实际触发类型为 `workflow_dispatch`。
- 失败 stage7 job `136631456620` 没有上传 tree，不能作为 donor。精确 donor artifact 为 `14678246702`，8,387,231,453 bytes，ZIP SHA256 `8bf03bc2379f957fb174751bccf956db9b8a64fa539d2852a187fa7652d9d6a3`。
- ZIP 内 `tree.tar.zst.001` 为 8,387,231,195 bytes，SHA256 `f0b4a8429ff4ee30de296ca0665495deb6205db3973a90bfa14552f88c5c7d85`。完整 ZIP SHA/CRC、zstd checksum、tar 全流和尾部均验证通过，实际全量解包约 24GB，而非只提取头部或部分源码。
- donor 已含 0236/0237 ICU 修复，真实历史 selection 是 **230→231**，只增加 0238 的 `base/version.h` include；唯一源码差异为 `components/embedder_support/user_agent_utils.cc`。不使用 ARM 的 228→231 三文件假设。
- `build/macos/mac157-x64-stage6-checkpoint.json` 固定完整 source receipt（170 outputs，加 `chrome/VERSION` 共171个源码输入）、core/platform、目标架构、准备 key、所有 Ninja graph、args、log/deps 和 PGO。真实树的 private full reverse/forward replay 与原子迁移通过，`out/Default` 的104,270项、4,972,308,650 bytes 的内容/模式/mtime/link inventory 前后相同。
- 压缩测试 fixture 来自这次真实 archive，保留完整 patch 源码面、真实 Version headers、producer metadata 和实测迁移记录；它不是完整编译树。UA pre/postimage 与既有 Linux 完整 translation-unit 的失败→成功证据一致，不引入假 header/API。本地 ARM Mac 对 Intel compile 的 native architecture gate 实测拒绝；没有声称本地 Intel TU 编译、完整链接或 runtime 验证通过。
- compiler failure 只有通过当前 run/attempt/stage 的 after-source gate 及再次 checkpoint verify，且 snapshot 成功，才允许上传失败树。源码、包、架构、版本和 SHA 仍是硬门槛，runtime 诊断非 blocking。此入口仅续编/上传候选资产，不执行统一 release。
