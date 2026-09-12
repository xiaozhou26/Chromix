# Fingerprint regression gate

## Goal

分别验证补丁内容、恢复源码、启动能力与实际接口行为，不用 stock Chrome、旧二进制或 ready stamp 替代当前构建证据。

## Plan

1. 编译前核验实际源文件，防止继续编译旧缓存中的失效补丁。
2. 打包后固定解包 native executable 的 hash、完整版本和探针输入。
3. 所有独立 suite 都运行；失败、超时和缺报告不能掩盖后续诊断。
4. 诊断与发布 ZIP 分开上传，保持发布器只接收 ZIP + SHA256SUMS 的契约。

## Implementation

### 编译前：源码凭据

完成 patch、migration 和 domain substitution 后运行：

```powershell
python -X utf8 tools/verify_patch_stack.py `
  --src C:/build/src --repo D:/C++/Chromix `
  --core C:/build/tooling/ungoogled-chromium `
  --platform-tooling C:/build/tooling/ungoogled-chromium-windows `
  --platform windows --output C:/diagnostics/source-new.json
```

输出必须是 SRC 外的新文件。校验器复制 patch targets 到 scratch，完整 reverse/
forward series，检查新增文件完整删除、逐文件 roundtrip、源 bytes/mtime 和输入
patch hash。CRLF 仅在副本内归一化。未完成标记、错版源码、symlink 或不匹配
hunk 直接失败，不修改 SRC/ready stamp。这是当前 hunks/新文件的结构证明，不是
完整 upstream 或二进制的签名证明。

Windows run `34614380682` 的 StrictNumeric 旧表达式有窄范围幂等迁移；新的
snapshot/display 补丁仍需干净匹配来源，不能靠重写缓存键升级旧源树。

### 打包后：二进制门禁

```powershell
python -X utf8 -m pip install -r tools/fingerprint-requirements.txt
$browser = 'C:/verified/chromix/chrome.exe'
$hash = (Get-FileHash -LiteralPath $browser -Algorithm SHA256).Hash.ToLowerInvariant()
python -X utf8 tools/fingerprint_acceptance.py `
  --browser $browser --expected-sha256 $hash --expected-version 152.0.7977.82 `
  --source-report C:/diagnostics/source-new.json --source-root C:/build/src `
  --output-dir C:/diagnostics/acceptance-new
```

依赖文件只安装测试驱动/解码器，不下载浏览器。必须传 native executable，不是
launcher、ZIP 或 archive hash。上例本地 hash 固定被测文件，不认证其来源。
CI 另负责同一构建的 package 校验和 source receipt 关联。

| Suite | 覆盖 |
|---|---|
| identity | 既有 UA/CH/locale/worker/restart 与基础 surface smoke |
| device | 五 context 执行、SAB/Atomics、GPU 操作、CDP font samples |
| canvas | 既有五 context、三次启动的 codec/color/alpha audit |
| runtime | CDP 几何、真实 OOPIF、时间、生命周期、音频及显式 fake media |
| display_backend | 无初始 CDP viewport，要求 launch UXR 后端生效，再覆盖/缩放 |
| transport | 回环 full ClientHello、ALPN、H2 SETTINGS/伪首部、JS/header |
| render | ImageBitmap、导出快照、worker ownership、WebGL loss、WebGPU boundaries |

每项默认 240 秒，可配置 30–1800 秒。Windows 使用 gated child + kill-on-close
Job Object；POSIX 使用独立进程组和 psutil 后代身份跟踪，只清理本次拥有的进程。
POSIX 后代发现依赖采样，不能宣称能追踪任意瞬间脱离父树的 daemon。缺报告、
启动异常、cleanup 错误和原始观测不符均失败。每项 JSON/log 与总报告保存退出码、
用时、hash、错误和 gap，不因其它 suite 失败而删除。

前后复核 binary、patch series、probe/helper、依赖版本和源码凭据。
`--source-root` 复核全部当前 patch target 的源 hash；ARM64 原生后验只消费同一
GitHub run 的 producer receipt，报告明确标为 `producer-receipt-only`。

### 结果语义

- `status=failed`：至少一项错误，退出非零。
- `status=incomplete`：必需检查通过，optional 能力有明确 gap。
- `ci_gate_passed=true`：非 control、source/binary 身份通过、七项完整执行且无
  required failure。允许记录的 optional gap，不表示完整设备验收。
- `full_acceptance=false`：没有真实硬件、font-file/glyph 或外部 proxy/DNS/
  QUIC/TURN 的完整证据，不自动升级为全量验收。
- `--control`：仅用于探针校准，无需 source receipt，永远不能通过 CI gate。

TLS fixture 关闭 session tickets，只比较完整握手，不删除 PSK extension 制造
一致。GREASE 值归一化但保留数量/有序向量位置；扩展排列随机化不当作失败。
Resumption、连接复用、外部路由和 QUIC 不属于该 fixture 的证明范围。

## Verification

```powershell
$env:CXX = 'C:/Program Files/LLVM/bin/clang++.exe'
$env:PATH = 'C:/Program Files/Git/usr/bin;' + $env:PATH
python -X utf8 -m pytest -q tools/tests/test_persona_snapshot.py `
  tools/tests/test_webgpu_restore_alignment.py tools/tests/test_verify_patch_stack.py `
  tools/tests/test_fingerprint_protocols.py tools/tests/test_fingerprint_runtime_audits.py `
  tools/tests/test_fingerprint_acceptance.py tools/tests/test_fingerprint_subprocess.py `
  tools/tests/test_fingerprint_corpus_review.py sdk/python/tests/test_persona.py
python -X utf8 tools/check_patches.py
npm test --prefix sdk/node
```

这些是算法、验证器和编排测试，不是 Chromium 编译。Chrome 153 control 完整跑完
七项：device、TLS/H2、extended render 通过，identity/Canvas 和两种 runtime 模式
失败。真实 OOPIF 屏幕/DPR 断言保留失败；stock Chrome 本身不实现 launch UXR
后端。结果不能作为匹配 Chromix 152 的验收通过。
