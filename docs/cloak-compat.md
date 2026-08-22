# CloakBrowser → Fortress 无缝切换指南

Fortress 的 SDK 提供 **CloakBrowser 兼容 API**:函数名、参数名、返回类型
(Playwright `Browser` / `BrowserContext`)、环境变量名全部对齐,现有 CloakBrowser
脚本**只改 import 即可运行**。

## Python

```diff
- from cloakbrowser import launch, launch_context, launch_persistent_context
+ from tilion_fortress import launch, launch_context, launch_persistent_context
```

```python
from tilion_fortress import launch

browser = launch(proxy="http://user:pass@proxy:8080", geoip=True, humanize=True)
page = browser.new_page()
page.goto("https://bot.sannysoft.com")
page.mouse.click(240, 300)      # humanize=True 时自动走贝塞尔轨迹
browser.close()
```

可用函数(与 cloakbrowser 一致):

| 函数 | 说明 |
|---|---|
| `launch(**opts)` | 返回 Playwright `Browser` |
| `launch_async(**opts)` | 异步版,`await launch_async()` |
| `launch_context(**opts)` | 返回 `BrowserContext`(viewport/locale/color_scheme 预置) |
| `launch_context_async(**opts)` | 异步版 |
| `launch_persistent_context(user_data_dir, **opts)` | 持久化 profile |
| `launch_persistent_context_async(user_data_dir, **opts)` | 异步版 |
| `build_args` / `get_default_stealth_args` | 参数组装(随机 seed + 平台声明) |
| `maybe_resolve_geoip(geoip, proxy, tz, locale, args)` | 出口 IP → (tz, locale, exit_ip) |
| `ensure_binary` / `clear_cache` / `binary_info` / `check_for_update` | 二进制管理 |
| `HumanConfig` / `resolve_human_config` | 行为层配置(preset: `default` / `careful`) |
| `ProxySettings` | Playwright 形态的代理 TypedDict |

参数(`headless, proxy, args, stealth_args, timezone, locale, geoip, humanize,
human_preset, human_config, extension_paths, license_key, browser_version,
release_channel, user_agent, viewport, color_scheme`)与 CloakBrowser 同名同义。
`**kwargs` 透传给 `playwright.chromium.launch()` / `browser.new_context()`。

环境变量兼容:`CLOAKBROWSER_BINARY_PATH`(直接指定本地 chrome 二进制)、
`CLOAKBROWSER_CACHE_DIR`(未实现,使用 Fortress 自己的缓存)、
`CLOAKBROWSER_VERSION` / `CLOAKBROWSER_RELEASE_CHANNEL`(钉版本/通道)、
`CLOAKBROWSER_GEOIP_TIMEOUT_SECONDS`。`license_key` 参数被接受但忽略
(Fortress 只有一个开源档)。

## Node

```diff
- import { launch, launchContext, launchPersistentContext } from "cloakbrowser";
+ import { launch, launchContext, launchPersistentContext } from "tilion-fortress";
```

选项为 camelCase(`stealthArgs, humanPreset, humanConfig, extensionPaths,
browserVersion, releaseChannel, contextOptions, launchOptions, userDataDir`),
返回 `playwright-core` 的 Browser/BrowserContext。需要 `npm i playwright-core`。

## 有意的差异

1. **geoip**:走 ip-api.com(HTTP,免依赖),不是本地 GeoLite2 数据库;语义相同
   ——显式 `timezone=`/`locale=`(或 args 里的裸 flag)永远优先。
2. **license**:无 Pro 档;`license_key` 静默忽略。
3. **user_agent 警告**:context 级 UA 仿真会让 UA 与 UA Client Hints 脱钩
   (可检测)。兼容层接受该参数但打印警告;引擎原生方案是 `--uxr-ua-*`。
4. **JS 侧无 puppeteer 子路径**(CloakBrowser 有 `cloakbrowser/puppeteer`),
   用 playwright 表面。
5. **Widevine**:`launch(widevine=True)` / `Fortress(widevine=True)` /
   `CLOAKBROWSER_WIDEVINE=1` 启用。CDM 来源:显式目录
   (`CLOAKBROWSER_WIDEVINE_CDM` 环境变量)→ 已安装的 Google Chrome(直接引用,
   不复制)→ Linux 下自动从 stable deb 提取。命令行工具
   `tools/fetch-widevine.py` 供 Docker/CI 预下载。引擎侧由补丁 0048
   (`--uxr-widevine-cdm=<dir>`)激活,不需要 Google API keys。

## 底层对接

兼容层发出的全部 `--fingerprint-*` 开关由引擎 `chrome_main.cc` 的 0036 补丁
归一化为 `--uxr-*` persona 开关,浏览器进程把 persona 经 mojo IPC 送进渲染进程。
`--fingerprint-webrtc-ip=auto` 会解析代理出口 IP 并注入(0042 补丁在引擎里把
srflx candidate 地址伪造成该 IP)。

## 验证

```bash
python -m pytest sdk/python/tests/test_cloak_compat.py   # 15 项
node sdk/node/test-cloak.mjs                             # 18 项
```
