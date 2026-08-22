# fortress-serve — 多 seed CDP 前端(cloakserve 式)

单端口对外、内部按 seed 维护一组**互相隔离**的浏览器实例。每个 seed 对应
独立的浏览器进程:独立的 `--fingerprint` 种子(canvas/音频噪声)、独立的
profile 目录、独立的调试端口。服务本身把 CDP 的 HTTP 发现端点(`/json*`)
反向代理给对应实例,并把 WebSocket 帧做字节级中继,`webSocketDebuggerUrl`
重写为指向本服务并携带 seed。Playwright / Puppeteer / browser-use 等
客户端**不需要任何改动**。

## 启动

```bash
python tools/fortress-serve.py --bundle /path/to/tilion-fortress \
    [--port 9333] [--max-pool 8] [--idle-timeout 900] [--proxy http://host:port]
```

- `--max-pool`:最大并发实例数,超出后淘汰最久未用的(默认 8)
- `--idle-timeout`:实例空闲回收秒数,0 关闭(默认 900)
- `--proxy`:所有实例共用的上游代理

## 使用

```python
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    a = p.chromium.connect_over_cdp("http://127.0.0.1:9333/?seed=1001")
    b = p.chromium.connect_over_cdp("http://127.0.0.1:9333/?seed=1002")
    # a 和 b 是两台"不同的机器":不同指纹噪声、不同 profile、互不共享状态
```

不带 `?seed=` 时自动生成随机 seed(一次性实例)。`GET /` 返回服务状态和
活跃 seed 列表。

## 架构说明

- 人设是**进程级**的(引擎的 `UxrConfig` 经 mojo IPC 进入渲染进程),所以
  per-seed 隔离 = per-进程隔离,这是池模型的根本原因,也是和单进程多
  context 方案的本质区别(context 之间会共享 persona)。
- WebSocket 中继是纯字节管道:升级握手后双向 `recv/sendall`,不解析帧,
  透明支持 CDP 的全部域(含 binary 帧)。
- 目标路由:`/devtools/<type>/<uuid>` 通过向各实例查询 `/json/list` 建立
  uuid→实例 缓存后直连对应上游。
- 测试:`python -m pytest tools/tests/test_fortress_serve.py`(用假上游
  驱动真实代理代码,含 WS 回环)。
