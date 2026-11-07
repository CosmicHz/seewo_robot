# 迁移指南（v1 → v2）

## 0. 结论先行

> **v1 零破坏性变更；v2 / 协议端点有破坏性调整。**
>
> - **v1**：`/api/*` 的响应格式、行为、状态码全部保持不变；现有 `client.py` / `tui_client.py` / `test/test_api.py` 不需要任何修改。
> - **v2 / OneBot / Satori**：以下行为已收紧，接入方需要适配 ——
>   1. **必须携带令牌**：复用 `config.json:api_key`。OneBot 用 `Authorization: Bearer` 或 `?access_token=`（缺失 401，不符 403）；Satori HTTP 用 `Authorization: Bearer`（401）；Satori WS 在 `IDENTIFY.token` 中提供。
>   2. **非法参数返回 400 `invalid_request`**：`count`/`limit`/`offset` 越界或非整数、请求体不是 JSON 对象、`segments` 含非对象元素。
>   3. **v2 历史写入独立文件** `chat_history.v2.json`，不再与 v1 的 `chat_history.json` 共用。
>   4. v2 的已发送消息 `id` 为空串（希沃不返回服务端 id）；OneBot `send_msg` 的 `message_id` 为 `null`。

---

## 1. 为什么加 v2 而不是改 v1

| 问题 | v1 现状 | v2 解法 |
| --- | --- | --- |
| 响应形状不统一 | 不同端点返回不同结构（有的 `{"status":"ok",...}`，有的直接是数组） | 统一信封 `{"status": "ok", "data": ...}` |
| 错误表达不统一 | 靠 `status` 字段 + `message` 文本区分 | 统一 `error.code` 枚举 + HTTP 状态码一一对应 |
| 无类型定义 | 只能读源码知道返回什么 | `seewo/interfaces/openapi.yaml`（OpenAPI 3.1）+ `seewo/sdk.py` 类型化客户端 |
| 平台语义泄漏 | 返回体里带 `resUrl` / 数字 `type` / `sender` 字符串 | 平台中立模型（`Segment` / `Message`），平台细节收进 `native` 字段 |
| 无事件出口 | 只能轮询 | SSE（`GET /api/v2/events`）+ OneBot 11 / Satori 协议推送 |

**为什么不直接改 v1**：v1 已被 `client.py`、`tui_client.py`、`test/test_api.py` 和可能存在的外部脚本依赖。改它等于破坏现有功能 —— 明确不做。

---

## 2. 并存关系

```text
/api/*          v1（原样保留，行为不变）
├── /api/status
├── /api/messages
├── /api/send
├── /api/send_image
├── /api/send_audio
├── /api/history
├── /api/load_earlier
├── /api/sync_all
├── /api/refresh
├── /api/config/reload
├── /api/login/qrcode
└── /api/login/status

/api/v2/*       v2（新增）
├── /api/v2/status          GET
├── /api/v2/capabilities    GET
├── /api/v2/messages        GET  实时拉取
├── /api/v2/messages        POST 发送
├── /api/v2/history         GET
├── /api/v2/sync            POST
├── /api/v2/events          GET  SSE 事件流
└── /api/v2/connection      POST

/onebot/v11/*   OneBot 11 协议端点（新增）
/satori/v1/*    Satori 协议端点（新增）
```

两个版本**并存但存储独立**：v1 用 `chat_history.json`，v2 用 `chat_history.v2.json`，v2 不导入 v1 记录；v2 REST 与 OneBot/Satori 之间共享同一个 `MessageService`。

---

## 3. 端点对照

| 用途 | v1 | v2 | 差异 |
| --- | --- | --- | --- |
| 查状态 | `GET /api/status` | `GET /api/v2/status` | v2 增加 `capabilities` 与 `connection.state` |
| 查能力 | — | `GET /api/v2/capabilities` | **v2 新增** |
| 拉最新消息 | `GET /api/messages?count=N` | `GET /api/v2/messages?count=N` | v1 不落库；v2 走核心层，**会落库并去重** |
| 发消息 | `POST /api/send` | `POST /api/v2/messages` | v1 支持 `strategy`（长消息截断/拆分）；v2 发**消息段序列** |
| 发图片 | `POST /api/send_image` | `POST /api/v2/messages`（`segments` 里放 image 段） | v1 收 `file_path` / multipart；v2 收 URL |
| 发音频 | `POST /api/send_audio` | `POST /api/v2/messages`（`segments` 里放 audio 段） | 同上 |
| 读历史 | `GET /api/history?limit&offset` | `GET /api/v2/history?limit&offset` | v2 额外返回 `total` |
| 全量同步 | `POST /api/sync_all` | `POST /api/v2/sync` | v1 支持 `batch_size`/`delay` 防风控并写 v1 文件；v2 只拉**最新一页**写 v2 文件，不提供历史补拉 |
| 刷新会话 | `POST /api/refresh` | `POST /api/v2/connection` `{"action":"..."}` | v2 还可 `connect`/`disconnect` |
| 事件推送 | — | `GET /api/v2/events`（SSE） | **v2 新增** |

---

## 4. 请求 / 响应格式对照

### 4.1 发送消息

**v1**（只支持文本，长消息有专门策略）：

```http
POST /api/send
{"content": "你好", "strategy": "truncate"}
```

**v2**（通用消息段序列）：

```http
POST /api/v2/messages
{
  "segments": [
    {"type": "text", "text": "你好"},
    {"type": "image", "url": "https://example.com/a.png"}
  ],
  "conversation_id": "student-uid"
}
```

v2 也接受简写 `{"content": "你好"}`（自动包成单个文本段）。

> **v2 没有 `strategy`**：长文本的处理交给调用方。服务端只会对**超过平台上限**的文本抛 `CapabilityError`（HTTP 501 + `error.code = "capability"`），由客户端自行截断/拆分。这是刻意的 —— 拆分策略是业务决策，不该由服务端替第三方框架决定。

### 4.2 响应信封

**v1**（形状随端点而变）：

```json
{"status": "ok", "seewoCode": 200, "strategy": "truncate", "message": "发送成功"}
```

**v2**（统一）：

```json
{"status": "ok", "data": {"id": "...", "platform": "seewo", "segments": [...]}}
```

错误：

```json
{
  "status": "error",
  "error": {"code": "capability", "message": "平台 seewo 不支持 image 类型消息"},
  "data": null
}
```

### 4.3 消息格式

**v1**（带平台语义）：

```json
{
  "id": 12,
  "time": "2024-01-01 10:00:00",
  "content": "我在学校很好",
  "type": 1,
  "sender": "student",
  "senderName": "小明",
  "resUrl": ""
}
```

**v2**（平台中立）：

```json
{
  "id": "12",
  "platform": "seewo",
  "conversation_id": "student-uid",
  "user_id": "student-uid",
  "role": "user",
  "timestamp": 1700000000000,
  "segments": [{"type": "text", "text": "我在学校很好", "data": {}}],
  "native": {"seewoType": 1, "senderType": "student", "senderName": "小明"}
}
```

**关键变化**：

| v1 | v2 | 说明 |
| --- | --- | --- |
| `id: 12`（int） | `id: "12"`（str） | 平台消息 id 不保证是数字，统一用字符串 |
| `time: "2024-01-01 10:00:00"` | `timestamp: 1700000000000` | 统一为毫秒时间戳，不做本地时区格式化 |
| `type: 1` | `segments: [{type: "text", ...}]` | 数字 type 是希沃专有，改为**消息段序列** |
| `sender: "student"` | `role: "user"` / `user_id` | 希沃的 sender 语义泄漏，改成中立角色 |
| `resUrl` | `segments[].url` | 同上 |
| `senderName` | `native.senderName` | 平台专有字段收进 `native`，**不参与跨平台语义** |

> `native` 字段的设计意图：保留平台原始信息供调试与追溯，但**上层业务逻辑不应读它**。跨平台能力判断只看 `role` / `segments` / `platform`。

---

## 5. 错误码对照

v2 用统一错误码，HTTP 状态码一一对应：

| `error.code` | HTTP | 含义 | 典型触发 |
| --- | --- | --- | --- |
| `invalid_request` | 400 | 参数非法 | 缺 `segments` / 越界的 `limit` |
| `unauthorized` | 401 | API Key 不对 | `X-API-Key` 缺失或不匹配 |
| `need_login` | 401 | 需要重新扫码 | 凭证过期且刷新失败 |
| `not_found` | 404 | 资源不存在（预留，当前无端点返回） | 按 id 查消息 |
| `conflict` | 409 | 状态冲突（预留，当前无端点返回） | 重复消息 / 状态机非法跃迁 |
| `capability` | **501** | 平台不具备该能力 | 发图片但未配上传器；发希沃不支持的段 |
| `upstream` | **502** | 上游平台失败 | 希沃返回业务错误 |
| `timeout` | **504** | 上游超时 | 请求超时 |
| `internal` | 500 | 服务端内部错误 | 未预期异常 |

**`501` 是有意选的状态码**：能力缺失不是「服务器坏了」，而是「这台服务器**没有**这个功能」。用 501 让客户端能明确区分「换个客户端也不行」和「稍后重试」。

---

## 6. SDK 迁移

仓库里有两个 SDK，用途不同：

### 6.1 `client.py` —— v1 SDK（**不变**）

```python
from client import SeewoClient

c = SeewoClient()
c.send_text("你好")
c.send_image("./照片.png")  # 本地路径，内部完成上传
```

保持原样。

### 6.2 `seewo.sdk.SeewoRobotClient` —— v2 SDK（新增）

```python
from seewo.sdk import SeewoRobotClient
from seewo.models import Segment

with SeewoRobotClient(base_url="http://localhost:5001", api_key="xxx") as c:
    print(c.status())
    print(c.capabilities())

    # 发消息段序列
    c.send([Segment.text_seg("你好"), Segment.image("https://.../a.png")])

    # 或用便捷方法
    c.send_text("你好")
    c.send_image("https://.../a.png")
```

**与 v1 SDK 的差异**：

| | `client.py` | `seewo.sdk` |
| --- | --- | --- |
| 发送图片 | `send_image("./本地路径.png")` —— 内部上传 | v2 当前**不支持媒体发送**（`501 capability`），媒体请走 v1 端点 |
| 长文本 | 服务端按 `strategy` 截断/拆分 | 不处理，交给调用方 |
| 错误 | 返回 dict，看 `status` 字段 | 抛 `SeewoApiError`，带 `.code` / `.status_code` |
| 返回类型 | dict | 类型化的 `Message` / `SendResult` |

> **v2 SDK 不做文件上传**；并且服务的 v2 Provider 未接上传器（接上会让已鉴权客户端指定服务端任意本地文件上传到希沃云），因此 `send_image` / `send_audio` 在 v2 下返回 `501 capability`。发媒体请用 v1 的 `/api/send_image` / `/api/send_audio`。

---

## 7. 第三方框架接入

不经过 REST，直接说协议。见 [`INTEGRATION.md`](INTEGRATION.md)。

---

## 8. 兼容性保证

| 保证 | 验证方式 |
| --- | --- |
| v1 响应格式不变 | 既有测试全绿（`tests/test_api_*.py`） |
| v1 行为不变 | 同上 |
| v1 的 `chat_history.json` 不被 v2 改写 | v2 使用独立 `chat_history.v2.json`；`JsonStore` 遇到 v1 结构拒绝加载（`CorruptStoreError`），原文件不动。`tests/test_core_service.py` 有回归用例 |
| v2 历史不导入 v1 旧记录 | v2 历史从空开始；旧记录到 v2 结构的映射涉及 sender/type 语义，本次不做（未验证不放宽） |
| v1 的 `merge_messages` 不去重的既有行为保留 | 既有测试锁定（v2 的去重在核心层，不影响 v1） |
| `Message.type` 容忍 `int \| str` | 既有测试锁定（旧数据里两种都有） |

---

## 9. 不打算改的东西

以下是**刻意保留**的现状，不是遗漏：

| 项 | 原因 |
| --- | --- |
| v1 的 `merge_messages` 不去重 | 既有行为，且既有测试锁定。v2 的去重在核心层，不回灌到 v1 |
| v1 的 `type` 字段 `int \| str` 混合 | 旧数据里两种都有，强制转换有丢数据风险 |
| v1 的长消息 `strategy` | 现有客户端依赖；v2 另起一套 |
| `uploads.json` 按 basename 做 key | 主链路不读它，影响面有限（见 README「已知问题」） |
| 路径 A（独立脚本）不经过 v2 | 路径 A 是「直连希沃」的独立路径，v2 属于路径 B |

---

## 相关文档

- [`ARCHITECTURE.md`](ARCHITECTURE.md) —— 分层与依赖方向
- [`CAPABILITIES.md`](CAPABILITIES.md) —— 能力表与缺口
- [`INTEGRATION.md`](INTEGRATION.md) —— 框架接入实操
- [`../README.md`](../README.md) —— 用户向使用文档
