# 框架接入实操手册

本文说明如何把本服务接入主流聊天机器人框架。

## 0. 生态调研与选型结论

| 框架 | 定位 | 协议 | 本项目的关系 |
| --- | --- | --- | --- |
| **Satori** | 跨平台、跨框架的**统一消息协议**（Koishi 生态系） | Satori 协议（WS 信令 + HTTP API） | ✅ **已实现** |
| **OneBot** | 最流行的机器人框架通用协议（NoneBot2 / NapCat / Lagrange 等均实现它） | OneBot 11 | ✅ **已实现** |
| **NoneBot2** | Python 机器人框架 | 驱动适配器（消费 OneBot 11） | 通过 **OneBot 11** 接入，无需专门适配 |
| **Koishi** | 跨平台机器人框架 | 多协议插件 | 通过 **Satori** 接入 |
| **AstrBot** | 多平台 LLM 聊天机器人 | 平台适配器 | 通过 **OneBot 11** 接入 |
| **LangBot** | LLM 机器人应用平台 | 平台适配器 | 通过 **OneBot 11** 或 **Satori** 接入 |

### 选型策略

**统一核心 + 轻量适配器**。不按框架各写一套消息处理、状态管理和业务逻辑 —— 那会让每接一个框架就复制一遍核心逻辑，迟早漂移。

协议差异实际只落在两处：

1. **事件形状** —— 核心事件怎么翻译成框架认的 JSON
2. **API 动作** —— 框架怎么反过来调我们（发消息、查历史）

其余（去重、排序、游标恢复、并发、状态机、事件分发、连接生命周期）全部复用 `seewo/core`。适配器只做「翻译」。

**为什么选这两个协议**：

- **OneBot 11** 是事实标准，生态最广（NoneBot2 / AstrBot / NapCat / LLOneBot / Shamrock 等）。接了它就接了大半个生态。
- **Satori** 是**跨框架的统一层**（Koishi / LangBot 都在用），且它原生支持「一个后端服务多个平台账号」的多账号模型，比 OneBot 的单账号假设更适合本项目。

两者都**已实现且可离线验证**，不是空接口。

---

## 1. 通用前提

### 1.1 启动服务

```bash
uv run api_server.py
```

服务启动时会自动挂载：

| 路径 | 协议 |
| --- | --- |
| `/api/*` | v1 REST（**保持不变**，见 [`MIGRATION.md`](MIGRATION.md)） |
| `/api/v2/*` | v2 REST（统一信封 + OpenAPI） |
| `/onebot/v11/<action>` | OneBot 11 HTTP API |
| `/onebot/v11/ws` | OneBot 11 正向 WebSocket |
| `/satori/v1/<resource>.<method>` | Satori HTTP API |
| `/v1/events` | Satori WebSocket |

### 1.2 鉴权

| 协议 | 方式 |
| --- | --- |
所有端点共用 `config.json` 的 `api_key` 作为令牌，**每次请求实时读取**（`/api/config/reload` 换 key 后旧 key 立即失效）。

| 协议 | 方式 |
| --- | --- |
| v2 REST | `X-API-Key` 头（或 `?api_key=`），不符 → 401 `unauthorized` |
| OneBot 11 HTTP / 正向 WS | `Authorization: Bearer <api_key>` 或 `?access_token=<api_key>`。未提供 → HTTP 401，不匹配 → HTTP 403；WS 在握手阶段校验，不通过直接关闭 |
| Satori HTTP | `Authorization: Bearer <api_key>`（**不接受** query 参数），不符 → 401 |
| Satori 事件 WS | `IDENTIFY` 帧的 `body.token`。**IDENTIFY 通过之前不推送任何事件**；token 错误时回一个 `error` 事件帧后断开 |

> `api_key` 为空串时所有端点放行，仅适合本机调试。服务默认监听 `0.0.0.0`，部署时务必设置非默认的 `api_key`。

### 1.3 能力表先行

任何框架接入前，先看 `GET /api/v2/capabilities`。**希沃不支持群聊、撤回、提及** —— 适配层会明确拒绝而不是假装成功：

- OneBot：返回 `retcode=100`（不支持）
- Satori：返回 `{"error": "..."}`
- v2 REST：返回 HTTP `501` + `error.code = "capability"`

完整能力表见 [`CAPABILITIES.md`](CAPABILITIES.md)。

---

## 2. OneBot 11

### 2.1 HTTP API

```bash
curl -X POST http://localhost:5001/onebot/v11/send_msg \
  -H "Content-Type: application/json" \
  -d '{"message_type":"private","user_id":"u1","message":"你好"}'
```

**已实现的 action**：

| action | 说明 |
| --- | --- |
| `send_msg` / `send_private_msg` | 发消息（只支持 `message_type=private`） |
| `get_msg` | 按 `message_id` 取消息 |
| `get_login_info` | 登录信息 |
| `get_version_info` | 版本信息 |
| `delete_msg` | 撤回（希沃不支持 → `retcode=100`） |
| `get_group_list` | 群列表（希沃无群 → 空列表） |
| `get_stranger_info` | 陌生人信息 |
| `set_restart` | 不支持 → `retcode=100`（本服务不能被协议端重启，不假装成功） |
| `get_status` | `GET /onebot/v11/get_status` |

**响应形状**（OneBot 11 标准）：

```json
{"status": "ok", "retcode": 0, "data": {...}}
```

失败：

```json
{"status": "failed", "retcode": 1400, "msg": "..."}
```

**retcode 约定**：

| retcode | 含义 |
| --- | --- |
| `0` | 成功 |
| `1` | 异步成功 |
| `100` | **不支持**（能力缺口 / 未实现的 action） |
| `1400` | 参数非法 / 内部错误 |
| `1401` | 未提供令牌（HTTP 401）/ 希沃会话失效 |
| `1403` | 令牌不匹配（HTTP 403） |

### 2.2 消息格式（段数组 / CQ 码字符串）

OneBot 11 的 `message` 字段支持**字符串**与**段数组**两种形态，本实现两种都接受，出站统一用**段数组**。

- 字符串默认按 **CQ 码**解析（`[CQ:image,file=...]`，含 `&#91;` / `&#93;` / `&#44;` / `&amp;` 反转义）
- 请求带 `auto_escape=true` 时整串按纯文本处理
- 未识别的 CQ 码 / 段类型不会当文本发出，由能力闸门拒绝（`retcode=100`）

| 平台中立段 | OneBot 段 |
| --- | --- |
| 文本 | `{"type": "text", "data": {"text": "..."}}` |
| 图片 | `{"type": "image", "data": {"file": "<url>", "name": "..."}}` |
| 音频 | `{"type": "record", "data": {"file": "<url>", "name": "..."}}` |
| 视频 | `{"type": "video", "data": {"file": "<url>", "name": "..."}}` |
| 文件 | `{"type": "file", "data": {"file": "<url>", "name": "..."}}` |
| 提及 | `{"type": "at", "data": {"qq": "<user_id>", "name": "..."}}` |
| 引用 | `{"type": "reply", "data": {"id": "<message_id>"}}` |

出站事件的 `raw_message` 字段同时给出可读形态：文本段给原文，媒体段给 `[CQ:image]` 这样的占位串（**仅供展示，不是可回传的编码** —— 回传请用 `message` 字段）。

### 2.3 正向 WebSocket

客户端连 `ws://localhost:5001/onebot/v11/ws?access_token=<api_key>`（或带 `Authorization: Bearer` 头），连上后立刻收到一条 `meta_event` / `lifecycle` 事件，之后推送消息事件。

> **本服务没有后台轮询**：希沃是拉取式平台，入站消息只在有人调用 `POST /api/v2/sync` 或 `GET /api/v2/messages` 时产生，届时才推送给 WS 客户端。需要持续接收时，由外部调度器定时调 `/api/v2/sync`（频率自行控制，防风控）。

```json
{
  "post_type": "message",
  "message_type": "private",
  "sub_type": "friend",
  "message_id": "12",
  "user_id": "student-uid",
  "message": "我在学校很好",
  "raw_message": "我在学校很好",
  "sender": {"user_id": "student-uid", "nickname": "测试学生"}
}
```

WS 上客户端也可以**反向调 API**（OneBot 11 的「API over WS」用法）：发 `{"action":"send_msg","params":{...},"echo":"..."}`，服务端回 `{"status":"ok","retcode":0,"data":...,"echo":"..."}`。

### 2.4 接入示例

**NoneBot2**（消费 OneBot 11）—— 安装 `nonebot-adapter-onebot` 并注册 V11 适配器，使用带 WebSocket 客户端能力的驱动器，在 `.env` 中配置正向 WS：

```dotenv
DRIVER=~fastapi+~websockets
ONEBOT_WS_URLS=["ws://localhost:5001/onebot/v11/ws"]
ONEBOT_ACCESS_TOKEN=<config.json 的 api_key>
```

配置项名称以 [NoneBot OneBot 适配器文档](https://onebot.adapters.nonebot.dev/docs/api/v11/config) 为准（`onebot_ws_urls` / `onebot_access_token`）。本项目未在真实 NoneBot2 实例上联调。

**AstrBot** —— 添加 OneBot 11 平台，正向 WS 地址填 `ws://localhost:5001/onebot/v11/ws`。

---

## 3. Satori

### 3.1 WebSocket 信令

客户端连 `ws://localhost:5001/v1/events`。（带 `/{platform}/{account}` 前缀的形式**只对 HTTP API 有效**，事件 WS 只注册了 `/v1/events`。）

**opcode**：

| op | 名称 | 方向 |
| --- | --- | --- |
| 0 | EVENT | 服务端 → 客户端 |
| 1 | PING | 客户端 → 服务端 |
| 2 | PONG | 服务端 → 客户端 |
| 3 | IDENTIFY | 客户端 → 服务端 |
| 4 | READY | 服务端 → 客户端 |

**握手流程**：

```text
客户端 ──IDENTIFY {token, sn?}──▶ 服务端
                                 ├─ token 不匹配 ──▶ EVENT{type:"error"}，随后断开
                                 └─ token 匹配   ──▶ READY{logins, proxy_urls}
```

`IDENTIFY` 带 `sn` 时，服务端只把 `sn` 计数推进到该位置，之后的新事件 `sn` 继续递增。**本服务不补发断线期间的事件**（不保留已推送事件的缓冲）；断线期间的消息请通过 `GET /api/v2/history` 补拉。

**事件形状**：

```json
{
  "op": 0,
  "body": {
    "sn": 1,
    "type": "message-created",
    "timestamp": 1700000000000,
    "login": {"sn": 1, "user": {"id": "default", "name": "seewo-robot"}, "platform": "seewo"},
    "message": {
      "id": "12",
      "content": "我在学校很好",
      "created_at": 1700000000000,
      "user": {"id": "student-uid", "name": "测试学生"},
      "channel": {"id": "student-uid", "type": "direct"}
    }
  }
}
```

**生命周期事件**：`login-added`（连接建立）、`login-removed`（断开）、`error`。

### 3.2 元素语法

| 平台中立段 | Satori 元素 |
| --- | --- |
| 文本 | 纯文本（HTML 转义） |
| 图片 | `<img src="..." title="..."/>` |
| 音频 | `<audio src="..." duration="3000"/>` |
| 视频 | `<video src="..."/>` |
| 文件 | `<file src="..."/>` |
| 提及 | `<at id="u1" name="Bob"/>` |
| 引用 | `<quote/>` |

属性支持单引号与双引号两种写法，解析时都会 `html.unescape`。

### 3.3 HTTP API

Satori 用 `{resource}.{method}` 点分路径：

```bash
curl -X POST http://localhost:5001/satori/v1/message.create \
  -H "Authorization: Bearer <api_key>" \
  -H "Content-Type: application/json" \
  -d '{"channel_id":"student-uid","content":"你好"}'
```

也支持带平台/账号前缀的完整形式：

```
POST /{platform}/{account}/satori/v1/message.create
```

**已实现的方法**：

| 方法 | 说明 |
| --- | --- |
| `message.create` | 发消息。返回 Satori `Message[]`（`{id, content, channel, user?}`）。希沃不返回服务端 id，`id` 为空串 |
| `message.get` | 按 `message_id` 取消息 |
| `message.delete` | 撤回（希沃不支持 → `{"error": ...}`） |
| `channel.get` | 会话信息 |
| `user.get` | 用户信息 |
| `session.create` | 会话信息 |

### 3.4 接入示例

**Koishi / LangBot** —— 填 Satori 适配器，WS 地址 `ws://localhost:5001/v1/events`。

---

## 4. 自己写适配器

要接一个本项目还没支持的协议，只需实现一个适配器 + 一个 Server。

### 4.1 适配器

继承 `IntegrationServer`，实现两个方法：

```python
from seewo.integrations.base import IntegrationServer


class MyAdapter(IntegrationServer):
    def translate_event(self, event: Event) -> Any | None:
        """核心事件 → 协议载荷。返回 None 表示不推送该事件。"""
        if event.type is EventType.MESSAGE and event.message is not None:
            return {"my_protocol": "message", "id": event.message.id}
        return None

    def send_payload(self, conn: Any, payload: Any) -> bool:
        """把载荷推给一个连接。失败返回 False。"""
        try:
            conn.send(payload)
            return True
        except Exception:
            return False
```

**免费获得**的东西（不用重写）：广播、重连退避、断线续传游标、事件订阅、序列化派发。

若协议需要额外包一层信封（如 Satori 的 `{"op":0,"body":...}`），覆写 `build_payload()` 而不是 `translate_event()`：

```python
def build_payload(self, event: Event) -> Any | None:
    body = self.translate_event(event)
    return {"op": 0, "body": body} if body else None
```

### 4.2 Server

继承 `IntegrationServer` 的服务端基类，注册 HTTP 路由与 WS 端点。参考 `seewo/integrations/onebot11/server.py`。

### 4.3 接一个全新平台（不是协议，是消息源）

实现 `Provider` 子类即可，**核心层、接口层、集成层零改动**：

```python
from seewo.provider.base import OutgoingMessage, PollResult, Provider


class MyProvider(Provider):
    platform = "myplatform"

    def __init__(self):
        super().__init__()
        self.capabilities = PlatformCapabilities(send_text=True, max_text_length=1000)

    def poll(self, cursor: str | None = None, limit: int = 50) -> PollResult: ...

    def _send(self, outgoing: OutgoingMessage) -> Message: ...
```

注意：子类实现的是 `_send()`（平台专有部分），**不要**覆写 `send()` —— 那个是模板方法，能力闸门在里面。

然后把它接到 `MessageService`，其余全部照旧工作。

---

## 5. 离线验证

协议适配层**不需要**真实希沃账号即可测试。用 `FakeProvider`：

```python
from seewo.testing.fake_provider import FakeProvider
from seewo.core.service import MessageService

provider = FakeProvider()
provider.emit_text("测试消息", message_id="m1")
service = MessageService(provider)
service.connect()
service.poll_once()
```

对应的测试见 [`../tests/test_integrations.py`](../tests/test_integrations.py)（协议形状）与 [`../tests/test_provider_contract.py`](../tests/test_provider_contract.py)（Provider 契约）。

---

## 6. 相关文档

- [`ARCHITECTURE.md`](ARCHITECTURE.md) —— 分层与依赖方向
- [`CAPABILITIES.md`](CAPABILITIES.md) —— 能力表与缺口
- [`MIGRATION.md`](MIGRATION.md) —— v1 → v2 迁移
