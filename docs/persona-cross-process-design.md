# Persona cross-process design

本文区分**已实现的配置/显示链路**与仍未实现的网络、设备和图形后端工作。验收状态见 [`FINGERPRINT_STATUS.md`](../FINGERPRINT_STATUS.md)。

## Goal

同一次浏览器启动使用一份不可变配置；页面、renderer 和 worker 不各自重新抽取身份值。配置身份与实际后端能力保持一致，而不是只改 API 返回值。

## Plan

1. 先验证再发布，避免并发读取到半份配置。
2. 在同一个 renderer Mojo 接口上先送快照、再初始化 renderer。
3. 显示配置进入实际布局/屏幕信息管线，不逐个改 getter。
4. 协议、媒体和图形保留真实能力；探针通过不等于后端仿真已经完成。

## Implementation

### Launch-scoped snapshot

`base/uxr_config.{h,cc}` 保留现有字符串键值兼容性，增加：

- `kSchemaVersion = 1`、`SetAll(config, schema_version)`。
- `IsInitialized()`、`Snapshot()`、`Display()`、`ValidationError()`。
- 输入数量、键名、单值/总字节数限制；拒绝 NUL、非有限显示数值、不成对尺寸和越界 work area。
- 第一次有效配置原子发布；相同配置幂等重放；不同配置/schema 被拒绝，原快照不变。
- typed getters 在失败/null 输出指针情况下不修改调用方输出。

Browser 在 `RenderProcessHostImpl::Init` 冻结 launch 配置，通过
`SetUxrConfig(map<string,string>, uint32 schema_version)` 发送给 renderer，
随后在同一 Mojo 接口调用 `InitializeRenderer`。**空配置也发送**。
Renderer 验证后安装快照，Blink/worker 的现有读取路径复用进程内只读实例。

这不是逐 BrowserContext 可变 persona，没有新增 V8 isolate 独立 snapshot 或
Network Service 配置 IPC。同一 launch 内的 BrowserContext 不支持相互冲突的
launch 配置；普通 CDP emulation 是另一层显式覆盖。

### 显示后端：0125–0128

`UxrDisplayConfig` 含屏幕/work area、viewport、native outer size、signed position、
DPR 和 orientation。默认零值保留 native 行为。显式色深声明和不存在的扩展屏幕声明被拒绝。

- `chrome_main` 将 outer size/position 接入 `window-size` / `window-position`。
  显式 native position 优先时移除对应 UXR 位置覆盖。
- Top-level widget 在 native visual properties 初始化后配置现有
  `ScreenMetricsEmulator`；已有 CDP emulator 或 auto-resize 时不覆盖。
- 显式 screen dimensions 产生一个 primary、nonextended 屏幕；work area 在
  后续 CDP screen dimensions 仍匹配 launch screen 时复用。
- 显式 viewport 是固定测试 viewport；未配置时跟随 native sizing。
- screen 事件、ScreenDetails、新建/更新 OOPIF 使用有效屏幕信息；top-level
  emulator 保留 original screen info 供 native compositor/popup 使用。
- 初始化只执行一次；清除 CDP 后不会在下次 resize 自动重装 launch defaults。

这是一屏 emulation，不是物理多屏虚拟化。CDP 的新 screen dimensions 优先；
同尺寸 override 不能单凭尺寸区分 work-area 意图。OOPIF 分数 DPR 下的布局、
合成、输入和弹窗仍需完整 Blink 构建与运行验证。

### 尚未实现的后端能力

- Network Service persona TLS/H2/QUIC 参数层；新增的是实际 TLS/H2 观测，
  不是改写 ClientHello 或传输行为。
- 新的虚拟 media-device manager。测试显式使用 Chromium 自带 fake device，
  权限由 browser 管理；`deviceId` 按 origin/profile，`groupId` 按 document
  salt，后者不是跨重启稳定标识。
- Canvas/WebGL/WebGPU 统一后端隐私变换、统一时钟量化或 graph-level 音频隐私。
- 系统字体文件到实际 glyph 的完整绑定、真实设备语料库和跨硬件能力仿真。

## Verification

- `tools/tests/test_persona_snapshot.py`：编译后的配置契约/并发测试，以及
  display 补丁在只读源文件副本上的 apply/reverse 检查。
- Python/Node 几何测试：uint64 向量、非法值、alias/work-area 冲突、viewport
  与 screen/DPR 联动；合成模板必须显式 opt-in。
- `tools/fingerprint_runtime_audit.py`：真实 OOPIF 与主页面的布局、CSS、
  visualViewport、ScreenDetails、resize/page-scale 一致性。
- `tools/verify_patch_stack.py` 和[指纹回归门禁](fingerprint-acceptance.md)：
  实际源码补丁、原生可执行文件身份和完整诊断流程。

单元测试和 apply/reverse 不替代 Chromium 编译。Chrome 153 control 的 OOPIF
屏幕/DPR 断言失败；没有匹配 Chromium 152 的新构建可证明原生补丁已生效。
