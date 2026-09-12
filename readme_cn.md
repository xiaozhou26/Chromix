# Chromix

[English](README.md) | 简体中文

[![Windows x64 构建](https://github.com/xiaozhou26/Chromix/actions/workflows/build-win-x64-github.yml/badge.svg)](https://github.com/xiaozhou26/Chromix/actions/workflows/build-win-x64-github.yml)
[![Linux x64 构建](https://github.com/xiaozhou26/Chromix/actions/workflows/build-linux-x64.yml/badge.svg)](https://github.com/xiaozhou26/Chromix/actions/workflows/build-linux-x64.yml)
[![Linux ARM64 构建](https://github.com/xiaozhou26/Chromix/actions/workflows/build-linux-arm64.yml/badge.svg)](https://github.com/xiaozhou26/Chromix/actions/workflows/build-linux-arm64.yml)
[![macOS Intel 构建](https://github.com/xiaozhou26/Chromix/actions/workflows/build-macos-x64.yml/badge.svg)](https://github.com/xiaozhou26/Chromix/actions/workflows/build-macos-x64.yml)
[![macOS ARM 构建](https://github.com/xiaozhou26/Chromix/actions/workflows/build-macos-arm64.yml/badge.svg)](https://github.com/xiaozhou26/Chromix/actions/workflows/build-macos-arm64.yml)
[![GitHub Release](https://img.shields.io/github/v/release/xiaozhou26/Chromix?display_name=tag)](https://github.com/xiaozhou26/Chromix/releases)

Chromix 是基于 Chromium 的浏览器项目，面向浏览器自动化、兼容性测试、隐私研究和受控的指纹实验。项目在固定版本的 `ungoogled-chromium` 及对应平台补丁之上，维护 Chromium 152 补丁集，并提供基于 Playwright 的 Python 和 Node.js SDK。

这里的 **persona（浏览器身份配置）** 指一次启动使用的平台、语言、时区等配置。项目关注这些配置在 JavaScript 接口、浏览器行为和实际能力之间的一致性；配置字段存在，不代表对应的完整设备模拟或运行时验收已经完成。

> 定制浏览器不等于“无法检测的自动化”。网络信誉、操作行为、账户历史以及网站自身的判断逻辑仍会影响结果。已实现的接口、已退役的覆盖项与尚未验证的能力，见 [指纹状态记录](FINGERPRINT_STATUS.md)。

## 主要特点

- **可配置的浏览器身份**：支持 UA、平台、语言、时区等设置，并维护 Canvas、WebGL、WebGPU、媒体、字体等相关补丁；各项能力以状态记录中的限制为准。
- **持久化配置种子**：SDK 的持久化用户目录复用同一个指纹种子；非持久化启动默认生成随机 32 位种子，命令行也支持显式种子。
- **Playwright 集成**：Python 返回 `Browser` / `BrowserContext`，Node.js 提供对应的 camelCase API，并保持 CloakBrowser 风格的常用接口。
- **代理感知配置**：可选 GeoIP 查询通过实际使用的代理获取语言、时区和出口 IP；代理启动默认限制非代理 UDP。WebRTC IP 参数修改本地地址的展示副本，实际 ICE 路由仍由原生后端负责。
- **五平台独立构建**：Windows x64、Linux x64/ARM64、macOS Intel/Apple Silicon 各自构建和验证，不因其他平台尚未完成而阻塞已验证平台。
- **固定源码与完整性检查**：源码版本和平台层固定，发布包附带 `SHA256SUMS`；SDK 在校验清单可用时先校验归档，再进行安全解压。

## 下载与平台支持

从 [GitHub Releases](https://github.com/xiaozhou26/Chromix/releases) 下载已经发布的包。源码目标平台如下，**实际可下载的平台以对应 Release 的 Assets 为准**，构建目标列表不表示所有包都已发布。

| 平台 | 归档名称 | 解压后的手动启动入口 |
|---|---|---|
| Windows x64 | `chromix-win-x64.zip` | `chromix/chromix.cmd` |
| Linux x64 | `chromix-linux-x64.zip` | `chromix/chromix` |
| Linux ARM64 | `chromix-linux-arm64.zip` | `chromix/chromix` |
| macOS Intel | `chromix-mac-x64.zip` | `chromix/chromix` |
| macOS Apple Silicon | `chromix-mac-arm64.zip` | `chromix/chromix` |

当前源码固定到 Chromium **`152.0.7977.82`**，对应发布标签为 [`v152.0.7977.82`](https://github.com/xiaozhou26/Chromix/releases/tag/v152.0.7977.82)。二进制发布可能滞后于源码。

同一 Chromium 版本的各平台通过构建、校验、解压、版本和无界面运行检查后，可以追加到同一发布标签。不同平台可能来自不同源码提交，具体来源以发布说明中的提交 SHA、工作流和运行记录为准。已有归档不会被同名但内容不同的文件替换。

**macOS 包没有 Developer ID 分发签名，也未公证。** 系统可能阻止打开下载的应用；编译器生成的临时签名不等于 Apple 分发签名。Linux 包仍依赖兼容的系统库和可用的 Chromium sandbox，并非完全静态二进制。

### Windows：校验并启动

从同一个 Release 下载 `chromix-win-x64.zip` 和 `SHA256SUMS`，在 PowerShell 中执行：

```powershell
$actual = (Get-FileHash .\chromix-win-x64.zip -Algorithm SHA256).Hash.ToLowerInvariant()
$lines = @(Get-Content .\SHA256SUMS | Where-Object { $_ -match '\s+\*?chromix-win-x64\.zip$' })
if ($lines.Count -ne 1) { throw "校验清单中缺少或重复记录了 Windows 归档" }
$expected = ($lines[0] -split '\s+')[0].ToLowerInvariant()
if ($actual -ne $expected) { throw "Chromix 归档 SHA-256 不匹配" }

Expand-Archive .\chromix-win-x64.zip -DestinationPath .\chromix-win-x64
.\chromix-win-x64\chromix\chromix.cmd
```

需要可复现的测试配置时，可以在启动入口后添加参数：

```powershell
.\chromix-win-x64\chromix\chromix.cmd `
  --fingerprint=123456789 `
  --fingerprint-platform=windows `
  --fingerprint-timezone=Europe/Berlin `
  --fingerprint-locale=de-DE
```

### Linux / macOS：校验并启动

下载对应架构的 ZIP 与同一发布版本的 `SHA256SUMS`。校验清单可能列出多个平台，下面只提取本次下载包的记录；清单记录不唯一或校验失败都会中止。

Linux x64 示例：

```bash
set -eu
archive=chromix-linux-x64.zip
awk -v name="$archive" '$2 == name || $2 == "*" name { print; count++ } END { if (count != 1) exit 1 }' SHA256SUMS > SHA256SUMS.selected
sha256sum -c SHA256SUMS.selected
unzip "$archive" -d chromix-linux-x64
./chromix-linux-x64/chromix/chromix --version
```

macOS Apple Silicon 示例：

```bash
set -eu
archive=chromix-mac-arm64.zip
awk -v name="$archive" '$2 == name || $2 == "*" name { print; count++ } END { if (count != 1) exit 1 }' SHA256SUMS > SHA256SUMS.selected
shasum -a 256 -c SHA256SUMS.selected
unzip "$archive" -d chromix-mac-arm64
./chromix-mac-arm64/chromix/chromix --version
```

Linux ARM64 或 Intel Mac 请分别替换为 `chromix-linux-arm64.zip`、`chromix-mac-x64.zip`，并同步修改解压目录。使用新的解压目录，保留执行权限以及 macOS framework 的符号链接。运行时使用普通用户和可用的 sandbox；不要通过追加 `--no-sandbox` 把启动失败当作验证通过。

如果下载的是 **Actions artifact**，先解开 GitHub 的外层归档，再校验内层浏览器 ZIP；内层 `SHA256SUMS` 不能用于校验外层下载文件。详细步骤见 [构建文档](BUILDING.md#verify-and-run-a-posix-candidate)。

## Python SDK

### 安装

```bash
python -m pip install chromix playwright
```

Linux 上如需安装 Playwright 的系统依赖，可以运行：

```bash
python -m playwright install-deps
```

要使用当前仓库中的 SDK，而不是 PyPI 版本，在仓库根目录执行：

```bash
python -m pip install ./sdk/python playwright
```

### 启动浏览器

```python
from chromix import launch

browser = launch(headless=False)
try:
    page = browser.new_page()
    page.goto("https://example.com")
    print(page.title())
finally:
    browser.close()
```

需要代理和 GeoIP 时，向 `launch` 传入 `proxy="http://user:pass@proxy.example:8080"` 与 `geoip=True`，并将示例代理替换为实际配置。GeoIP 可影响默认语言和时区，显式传入的设置优先。未指定本地浏览器时，SDK 会按所配置的版本和通道查找或下载二进制；对应平台尚未发布时，应使用下文的本地二进制方式。

### 持久化用户目录

```python
from chromix import launch_persistent_context

context = launch_persistent_context(
    "./profile",
    locale="zh-CN",
    timezone="Asia/Shanghai",
    headless=False,
)
try:
    page = context.new_page()
    page.goto("https://example.com")
finally:
    context.close()
```

异步 API 包括 `launch_async` 等变体，详见 [Python SDK 文档](sdk/python/README.md)。

### 二进制管理

```bash
python -m chromix --version
python -m chromix install
python -m chromix info
python -m chromix clear-cache
python -m chromix widevine
```

`widevine` 是 Linux x64 的辅助命令，不代表每个平台都具备相同的 DRM 能力。

## Node.js SDK

Node.js SDK 使用 `playwright-core`，包名为 **`@xiaoxiaofeihh/chromix`**。npm 上不带 scope 的 `chromix` 属于其他项目，请勿混用。

```bash
npm install @xiaoxiaofeihh/chromix playwright-core
```

将以下代码保存为 `.mjs` 文件，或在启用了 ES Modules 的项目中运行：

```javascript
import { launch } from "@xiaoxiaofeihh/chromix";

const browser = await launch({ headless: false });
try {
  const page = await browser.newPage();
  await page.goto("https://example.com");
  console.log(await page.title());
} finally {
  await browser.close();
}
```

代理与 GeoIP 使用 `proxy`、`geoip` 选项；持久化目录使用 `launchPersistentContext`。完整参数及兼容性差异见 [Node.js SDK 文档](sdk/node/README.md)。

```bash
npx chromix --version
npx chromix install
npx chromix info
npx chromix clear-cache
```

## 使用本地浏览器二进制

两个 SDK 都通过 **`CLOAKBROWSER_BINARY_PATH`** 指定本地浏览器并跳过发布包下载。路径必须指向实际可执行文件，而不是 ZIP、目录、Windows `.cmd` 或 macOS `.app` 目录，并保留旁边的完整运行时文件。

Windows：

```powershell
$env:CLOAKBROWSER_BINARY_PATH = "D:\chromix-build\src\out\Chromix\chrome.exe"
```

Linux：

```bash
export CLOAKBROWSER_BINARY_PATH="/absolute/path/chromix/chrome"
```

macOS：

```bash
export CLOAKBROWSER_BINARY_PATH="/absolute/path/chromix/Chromium.app/Contents/MacOS/Chromium"
```

在同一终端运行平常的 SDK 启动代码即可。请先校验所使用的包，并确保二进制与本机操作系统、架构匹配。

Python 包装层的 `chromix.launch(executable_path=...)` **不是受支持的本地路径选择方式**：包装层会先解析二进制，并自行传入 `executable_path`，重复提供会发生参数冲突。直接调用 Playwright 的 `chromium.launch(executable_path=...)` 是另一套 API。

Node.js 顶层 `executablePath` 也不是该包装层的下载绕过选项；`launchOptions.executablePath` 在 `ensureBinary()` 之后才生效。需要不下载的本地启动时，两种 SDK 都使用上述环境变量。

### 常用环境变量

| 变量 | 用途 |
|---|---|
| `CLOAKBROWSER_BINARY_PATH` | 使用本地可执行文件，跳过发布包下载 |
| `CLOAKBROWSER_VERSION` | 选择 SDK 中配置的浏览器主版本或版本通道 |
| `CLOAKBROWSER_RELEASE_CHANNEL` | 选择 `stable` 或 `latest` |
| `CLOAKBROWSER_GEOIP_TIMEOUT_SECONDS` | 设置 GeoIP 查询超时 |
| `CLOAKBROWSER_WIDEVINE_CDM` | 指向已有 Widevine CDM 目录 |
| `CLOAKBROWSER_WIDEVINE=0` | 关闭 Widevine 自动查找 |
| `CHROMIX_CACHE_DIR` | 覆盖 SDK 二进制缓存目录 |
| `CHROMIX_DOWNLOAD_HOST` | 覆盖发布资产下载主机 |

## 身份配置与能力边界

常用显式命令行参数：

```text
--fingerprint=<非零 uint64 种子>
--fingerprint-platform=linux|windows|macos
--fingerprint-timezone=<IANA 时区，例如 Asia/Shanghai>
--fingerprint-locale=<语言标签，例如 zh-CN>
--force-webrtc-ip-handling-policy=disable_non_proxied_udp
```

显式调用参数优先于 GeoIP 推导结果。需要长期稳定身份的测试，应复用同一个种子和用户目录；只有需要新身份时才更换种子。平台配置是声明的测试身份，不会把 Linux 主机变成真实的 Windows 或 macOS 设备。

完整参数表见 [指纹参数说明](docs/fingerprint-flags.md)：GPU、CPU/内存、屏幕/任务栏、品牌/版本、配额、Windows 字体度量、WebRTC IP/auto、noise/off、第三方 Cookie、Windows 声音表和 `FakeShadowRoot` 均已接入当前源码。**需要重编译；只更新 SDK 不会让旧二进制自动具备这些功能。**

当前实现的默认值和边界：

- 启用 `--fingerprint` 后默认 CPU/内存为 8/8，屏幕为 Windows/Linux 1920×1080、macOS 1440×900；任务栏高度分别为 48/0/95。页面 viewport 与 screen 是不同设置，SDK 不会因此自动套用旧的随机 viewport 模板。
- 存储配额接入浏览器后端，默认 102400 MiB；真实使用量、磁盘耗尽和桶限制仍保留。Network Information 与原生通知器保持一致。
- WebRTC 支持显式 IP 和 `auto`，GeoIP 可复用同次查询的出口 IP；不生成虚假候选或 STUN 成功，保留远端和 TURN relay 地址。修改候选字符串不等于改变流量路径。
- GPU 池包含 Windows/Linux/macOS **身份模板**，不是经过测量的完整设备数据库；公开身份参数保留真实 GL/Dawn 能力。
- 旧的独立 CPU/内存/屏幕随机池、GL 能力模板、字体替换和 Canvas 读回/导出噪声仍需 `--uxr-synthetic-device-tests=true`。`noise=false` 保留种子并关闭已有扰动，但不代表四套 Canvas/WebGL/audio/client-rect 噪声引擎已经实现。
- Windows 字体参数是有条件的字体度量对齐，不是完整 DirectWrite 模拟；跨系统声音表不安装 SAPI 合成引擎。第三方 Cookie 和 closed shadow DOM 访问均为显式选项。
- 屏幕与实际布局、字体来源、媒体后端、图形渲染以及 TLS/HTTP 等网络层的一致性，仍有未完成或未通过匹配浏览器验收的项目。

实现细节与验收边界以 [指纹状态记录](FINGERPRINT_STATUS.md)、[真实设备池说明](docs/device-pool.md) 和 [Canvas 链路说明](docs/canvas-chain.md) 为准。新增的 [指纹回归门禁](docs/fingerprint-acceptance.md) 在编译前核验实际补丁内容，解包后固定二进制 hash/版本运行七项测试，失败诊断单独保留。单元测试通过、补丁可应用或一个构建步骤成功，都不能替代匹配版本的真实浏览器验证。

### 高级实验选项

以下能力默认关闭，可能干扰自动化或削弱浏览器隔离：

- `--fingerprint-devtools-runtime-suppression`：抑制部分 V8 Runtime 可观测行为，可能影响控制台消息和自动化绑定。
- `--fingerprint-canvas-bridge=<host:port|ws://...>` 配合 `--fingerprint-canvas-bridge-unsafe`：实验性远端 Canvas Bridge；参与的渲染进程会失去 sandbox。当前 Canvas 路径还需要合成测试开关，且不代表完整的 WebGL 远端替换已经实现。

`--fingerprint-webrtc-ip` 已恢复为本地展示层参数，并增加启动前 `auto` 解析；`--fingerprint-webrtc-fake-srflx`、`--fingerprint-webrtc-fake-srflx-allow-udp` 及对应 `uxr` 参数仍然退役。解析失败不会回退直连，SDK 的 SOCKS 元数据查询能力也不等于 Chromium 新增了 SOCKS 认证支持。更多信息见 [参数说明](docs/fingerprint-flags.md) 和 [补丁说明](patches/README.md)。

## 从源码构建

源码层次固定为：**Chromium 归档 → ungoogled 核心补丁 → 对应平台补丁 → 二进制裁剪 → Chromix 补丁集**。

| 层次 | 固定版本 |
|---|---|
| Chromium | `152.0.7977.82` |
| ungoogled-chromium | `152.0.7977.82-1` |
| ungoogled-chromium-windows | `152.0.7977.82-1.1` |
| ungoogled-chromium-portablelinux | `152.0.7977.82-1` |
| ungoogled-chromium-macos | `152.0.7977.82-1.1` |
| Chromix | [patches/series](patches/series) 中的 146 个补丁 |

完整提交固定值见 [build/ungoogled-revisions.psd1](build/ungoogled-revisions.psd1)。

### Windows x64

需要 Visual Studio 2022 的 C++ 桌面开发工作负载、Windows 11 SDK 10.0.26100 及 Debugging Tools、Python 3、Git、PowerShell 7 和 7-Zip。约 120 GB 空闲磁盘只是起始估算，实际需求取决于源码、对象、快照和打包同时占用的空间。

在仓库根目录的 Developer PowerShell 中执行：

```powershell
pwsh build/windows/build.ps1 -WorkDir D:\chromix-build -Jobs 8
```

用同一工作目录继续中断的编译：

```powershell
pwsh build/windows/build.ps1 -WorkDir D:\chromix-build -Resume -Jobs 8
```

普通源码构建的输出为 `D:\chromix-build\src\out\Chromix\chrome.exe`。

### Linux / macOS

Linux 需要 Chromium 对应的 Debian/Ubuntu 构建依赖，以及 Python 3、Git、Ninja、Node.js、Go 和归档工具。以下示例在原生 x64 Linux 主机上执行：

```bash
build/build.sh /path/to/chromix-linux-build x64
build/linux/package-linux.sh /path/to/chromix-linux-build/src/out/Chromix /path/to/dist x64
```

macOS 需要 **Xcode 26+ 与 macOS SDK 26+**、命令行工具、Python 3、Git、Ninja、Node.js、Go 和归档工具；构建架构应与主机匹配。Apple Silicon 示例：

```bash
build/macos/build.sh /path/to/chromix-mac-build arm64
build/macos/package-macos.sh \
  /path/to/chromix-mac-build/src/out/Chromix/Chromium.app \
  /path/to/dist arm64
```

Intel Mac 将 `arm64` 替换为 `x64`。Linux/macOS 建议至少从 100 GB 空闲磁盘开始评估，这不是足够空间的保证。恢复上游完整构建树时输出可能位于 `out/Default`，手动打包必须使用对应路径。

### GitHub Actions 与缓存续编

五个平台分别使用独立工作流；POSIX 平台默认 `fast`、`staged`，可通过 `compile_jobs=auto|N` 控制编译并行度。`fast` 调整 ThinLTO 优化策略，不代表关闭 sandbox 或浏览器能力，也不保证指定的构建耗时。

缓存续编恢复的是经过校验的源码、构建状态及对象文件。源码、编译器、SDK 或依赖发生变化时，Ninja 仍会重编相关输出。**缓存下载成功不等于对象已被保留，也不等于已测得加速。** 跨阶段快照、下载摘要、SDK 内容指纹、对象保留报告和最终原生运行验证是不同的证据。

完整构建、缓存选择、失败恢复、源码迁移、打包及发布流程见 [BUILDING.md](BUILDING.md)。已经运行的 Actions 使用启动时的工作流提交，新文档或源码提交不会改变这些运行中的构建。

## 开发与验证

在仓库根目录运行：

```bash
python3 tools/check_patches.py
python3 -m unittest discover -s tools/tests -v
python3 -m pytest -q
npm --prefix sdk/node test
git diff --check
```

这些检查需要对应的开发依赖和平台工具；环境相关跳过不算验证通过。完整 Chromium 编译与浏览器集成验证仍需单独执行。

对于已经构建、来源独立核验过的本地浏览器，可运行：

```bash
python3 tools/fingerprint_smoke.py \
  --browser /path/to/verified/chromix/chrome \
  --platform linux \
  --locale zh-CN \
  --output /tmp/chromix-fingerprint-smoke.json
```

该工具需要 Python Playwright，使用自己的本地回环测试页面，不会自动下载浏览器。`--browser` 必须是实际可执行文件。发布归档的 SHA-256 与可执行文件的 SHA-256 是不同对象的摘要，不能混用；普通工具测试也不能当作匹配版本 Chromix 的运行验收。

## 仓库结构

```text
patches/          Chromium 身份配置与集成补丁
build/windows/    Windows 准备、分阶段构建和打包脚本
build/linux/      Linux 打包与 sandbox 辅助工具
build/macos/      macOS 构建、打包及可选公证辅助工具
build/posix/      Linux/macOS 缓存恢复与阶段交接脚本
sdk/python/       Python Playwright 包装层与二进制管理
sdk/node/         Node.js Playwright 包装层与二进制管理
tools/            补丁检查、构建辅助工具和回归测试
docs/             指纹一致性、设备池和设计说明
assets/fonts/     字体资源及其来源说明
```

## 许可证

Chromix 原创源码、补丁集成和 SDK 使用 [BSD 3-Clause License](LICENSE)。Chromium、第三方组件及字体保留各自的许可证和声明，BSD 许可证不会替代这些条款。字体来源与许可见 [assets/fonts/SOURCE.md](assets/fonts/SOURCE.md)。

## 项目状态与社区

项目持续开发中，Chromium 升级可能需要逐项调整补丁。使用固定发布标签并核对校验清单，有助于复现环境；单个平台的基础启动检查不代表全部指纹能力已经完成。

- [问题反馈](https://github.com/xiaozhou26/Chromix/issues)
- [LINUX DO](https://linux.do)
