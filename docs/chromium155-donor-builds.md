# Chromium 155 指定上游文件树构建

本分支用于用户提供的三个 `155.0.8059.39` 编译缓存。它与仍在处理的 macOS154 构建、已发布的154 Release分别管理，不把版本号替换等同于完整浏览器验收。

## 来源

Core gitlink 均为 `ffeb45a2bb88bc56a1dfed31410aeb10e9efcd5a`，版本 `155.0.8059.39`，revision 1。

| 平台 | 上游运行 | 平台提交 | 文件树 artifact |
|---|---|---|---|
| Linux x64 | `37906884923` | `025d99db1673a21992534551598b73ca79360762` | `11652570743` |
| Linux ARM64 | `37906884923` | 同上 | `11656090008` |
| Windows x64 | `37831345909` | `989b4382364baaddb8b3180e1eb1030ca96a7273` | `11670281902` |

- Linux x64 outer SHA-256：`34182b5fdbec949fbe29d320a7d7200eb9cd4b284c18b8daa0c516ab4816fa1a`。
- Linux ARM64 outer SHA-256：`a4c0bfd8020cd34f27a652ae449f9e8547a468b8b813a11a64acf8a0891db447`。
- Windows x64 outer SHA-256：`4648e4426093106521986dc5a4dc66b47d861250710b3391f5ed7cf73d11db5c`。

完整大小、时间和摘要以 `build/upstream-cache.json` 为准；下载时还必须验证 GitHub 返回的运行、生产者仓库、精确 artifact 身份和实际归档字节。

本分支没有 Windows ARM64 155 文件树；Windows source 只登记 x64，ARM64 请求必须明确拒绝，不把旧154文件树与155 pins混用。main 的已发布版本、SDK下载通道和Docker标签不因本分支适配而更新。

## 补丁适配

保持基础224补丁及历史152/154覆盖不变，新增 `patches/chromium155/`，由精确版本、core和overlay身份选择。

独立下载155固定源码目标文件，V8/WebRTC按其DEPS固定提交处理；应用目标相关core与平台覆盖后，分别测试未域名替换/已域名替换的Linux与Windows完整224补丁链，执行反向/正向回放。该检查证明目标源码的补丁可应用性，不代表全Chromium C++接口编译、链接或运行验收完成。

155的Canvas图像导出实现变化采用对应原生颜色转换路径；不能机械搬用154图像格式假设。噪声结果仍须对最终新浏览器执行 `tools/browser_feature_check.py`。

## 恢复工具

Linux生成器、TypeScript/esbuild外链例外和Windows MIDL loader只能接受已核验版本身份。新的原始源码和包摘要需要重新取得证据；未知字节保持拒绝，不能仅更新版本元组让旧摘要检查失效。

Chromium155固定源码的 `build/vs_toolchain.py` 仍要求 Windows SDK `10.0.28000.0`，因此扩展原有153/154的相同安装器映射到155，保留安装器SHA-256验证。

## 构建

在独立155分支调度以下三个工作流，显式设置 `use_upstream_cache=true`：

- `build-linux-x64.yml`：fast、staged、自动并行。
- `build-linux-arm64.yml`：fast、staged、自动并行，并在原生ARM64上验证最终包。
- `build-win-x64-github.yml`：native、自动并行、`upstream_run_id=37831345909`。

缓存失效、完整性失败或身份不符时禁止冷构建回退。失败后的续编应优先使用该次155运行最新有效检查点；154与155不能仅改配置直接跨版本复用Chromix层快照。

本次无授权声称网站检测效果、完整设备能力等价或发布门禁已通过；以实际构建、包校验、原生验收及像素诊断报告为准。

## macOS155 controller 链接修复

ARM64 run `39280602881` 的 continuation stage6 在 framework 链接时报
`ManagedProfileCreationController::CreateManagedProfileForTesting` 未定义，调用者是
`ProfileManagementDisclaimerService::OnRegisteredForPolicy`。Chromium `155.0.8059.39`
对应 `3ff7ac5a9224be9156d7f8703a06e22890aafd34`；固定 core 的
`core/ungoogled-chromium/remove-unused-preferences-fields.patch` 删除了 enterprise
`impl` 的 controller `.cc` source，但没有删除 consumer 中 testing factory 的调用。
`CHECK_IS_TEST()` 是运行时检查，不会免除链接依赖。固定 mac overlay
`b1238bb72b02cd36219991d9029c1c65183ea295` 的23个补丁均未修改这些目标文件。

`patches/chromium155-macos/0225-managed-profile-controller-link.patch` 与 Linux0225
字节相同，只恢复 `chrome/browser/enterprise/BUILD.gn` 的一行真实实现 source。
保留原 `is_linux || is_win || is_mac` 条件及所有 source、signin、testing 检查，
不添加stub、不取消调用、不改变链接器选项，也不回退core对真实实现的既有修改。
mac x64/ARM64使用同一追加补丁；Android/ChromeOS不进入该desktop source分支。

根224补丁及其历史字节不变。mac155有效序列从224变为225，只有精确mac155 pins才读取
`build/patch-additions-macos155.json`。mac154仍使用自己的quit manifest；
Windows155为231、Linux155为226，身份及preparation key均保持不变。
没有新manifest和新目录的历史mac155 checkout仍生成原224身份，必须由迁移工具额外认证
donor提交及检查点，不允许仅移除manifest绕过目标快照认证。

严格检查点迁移所需固定值（SHA-256）：

| 输入 | 摘要 |
|---|---|
| 新mac155 manifest | `94bb12f24f1c840f5a434bdc24971ab948be908944fd7c2fb51e6a39852af64f` |
| 新selector | `13e603e032b1854a52e905b6e77c4b6f89ad202f2710c1296087628fe0908396` |
| 基线selector | `44a9a15a798ddb53a59c64cb2669e89a47b283a3a5e877e8543e3eca404531a5` |
| 新0225补丁 | `350b333232d5865d9dff93a32b1e7b9c2e16a0c1fd31479809a0fd9106db700a` |
| enterprise BUILD.gn 修复前 | `7706d16e7d5a6f494f7ef2f3d7269fbc26aff03df989e562651f5689d120e71a` |
| enterprise BUILD.gn 修复后 | `7e21eff01a1f3925b9b13514731ac15d61c1678a68cae573c35e6bc4ef9168c9` |

本地基线 `1a7d4b9c8f4e7c3530534682b4c233275f1f99a2` 与远端
`77b940f4a5d72b2299f4bcb8776ee8aa5d8351ae` 经GitHub API确认tree同为
`62597df18e217d315c7ea7db32de0bd5fb576634`。这只是源码基线，续编须由独立迁移工具
选择最新有效检查点，不能默认回到原stage8。

聚焦测试覆盖manifest/patch篡改及符号链接拒绝、历史平台身份与key、真实155.39源码
正反向应用、domain substitution，以及原GN impl body在mac x64/ARM64与其他平台、
enterprise features开关下的实际GN求值。`CHROMIX_GN`可指定GN程序；
`CHROMIX_MACOS155_SOURCES`可指定独立的`pristine/core/mac`输入，执行完整225补丁链回放。
本次聚焦回归为189 passed、10 skipped；跳过项为其他平台/历史版本的可选独立源码回放。
mac155的150个固定原始文件已独立下载，完整225补丁链在两种domain模式均通过
`reverse-forward-in-scratch`验证，输出覆盖168个目标文件。旧stage7/stage8 checkpoint
测试另有7项因新recipe key被旧严格认证拒绝，须由检查点迁移接入处理，本修复不放宽门禁。
这些检查不代替完整Chromium编译、GN依赖闭包检查、framework链接和原生运行验收。
