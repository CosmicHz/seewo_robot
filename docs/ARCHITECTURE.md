# 架构说明

本文描述 `seewo/` 包的模块划分、依赖方向与关键设计约束。阅读完应能判断「一段新代码该放在哪一层」。

> 目标不是重写一个希沃机器人，而是建立**业务与协议充分解耦**、拥有稳定扩展接口、能方便接入主流聊天机器人生态的通用消息服务。

---

## 1. 分层与依赖方向

依赖**严格单向向下**，上层可以调用下层，下层**不得**反向 import 上层：

```text
┌──────────────────────────────────────────────────────────┐
│ interfaces/    对外 HTTP 接口（REST v2 / OneBot / Satori）│
├──────────────────────────────────────────────────────────┤
│ integrations/  第三方框架协议适配（OneBot 11 / Satori）   │
├──────────────────────────────────────────────────────────┤
│ core/          通用业务：去重/排序/游标/并发/状态/事件    │
├──────────────────────────────────────────────────────────┤
│ provider/      平台接入契约（抽象基类，无平台专有符号）  │
├──────────────────────────────────────────────────────────┤
│ providers/     具体平台实现（目前仅 seewo）               │
├──────────────────────────────────────────────────────────┤
│ models/ errors/  平台中立模型 + 异常体系（纯标准库）      │
└──────────────────────────────────────────────────────────┘
```

**唯一的例外**：`seewo/providers/seewo.py` 被允许 import 仓库根目录的希沃协议层（`api` / `login` / `msg` / `stu` / `upload`）。这是**复用既有协议实现**的刻意选择，不是破例 —— 详见 §5。

这条约定由 `tests/test_layering.py` **自动守卫**（AST 解析 import），违反即测试失败：

| 规则 | 检查方式 |
| --- | --- |
| `models` / `errors` / `provider.base` / `core.*` 不得 import 平台专有模块 | 白名单匹配 |
| `core` 不得 import `interfaces` / `integrations` / `providers` | 硬断言 |
| 只有 `providers/seewo.py` 可 import 希沃 DAO | 硬断言 |
| `models.py` 只依赖标准库 | 硬断言 |
| 每个模块只 import 白名单内的下层模块 | 逐模块白名单表 |
| 包内无逆向（向上）依赖 | 层号表 |
| 无 TODO / FIXME 残留 | 文本扫描 |
| `seewo/` 文件数 ≤ 30（防过度设计） | 计数 |

---

## 2. 各层职责

### 2.1 `models.py` — 平台中立模型

**只依赖标准库**，不含任何平台专有符号。可直接被任何第三方框架消费。

| 模型 | 说明 |
| --- | --- |
| `Segment` | 消息的最小单元。`type` 判别 + 扁平字段（非继承体系），便于 JSON 序列化与跨语言映射 |
| `Message` | 一条消息。`sort_key` = `(timestamp, id)` 复合键，保证同一毫秒内也有确定顺序 |
| `User` / `Conversation` | 用户与会话 |
| `Event` | 事件（消息、连接状态变化、错误） |
| `PlatformCapabilities` | **能力声明表** —— 「不伪造能力」的载体 |
| `ConnectionState` | 连接状态机，带**显式合法跃迁表** |
| `SegmentType` / `Role` / `ConversationType` / `EventType` | 枚举 |

`Segment.data` 字段用于承载平台专有附加数据，**原样透传、不参与跨平台语义** —— 平台细节被关在一个字段里，而不是散进模型各处。

### 2.2 `errors.py` — 异常体系

```text
SeewoError
├── ContractError            契约不满足
│   ├── CapabilityError      平台不具备该能力（带 .capability 字段名）
│   └── ProviderUnsupportedError
├── ProviderError            平台侧失败（带 .retryable）
│   ├── ProviderConnectionError
│   ├── ProviderAuthError
│   ├── ProviderBusinessError
│   └── ProviderTimeoutError
├── ServiceError
│   ├── SessionExpiredError
│   ├── DuplicateMessage
│   ├── StoreError → CorruptStoreError
│   ├── InvalidStateTransition
│   └── EventDispatchError
```

**关键设计**：平台错误码在 Provider 边界被翻译成**平台中立语义**（认证失效 / 业务拒绝 / 连接失败 / 超时）。上层因此不需要知道希沃的 `-500` 是什么意思。

### 2.3 `provider/base.py` — 平台接入契约

Provider 是**平台扩展的唯一入口**。一个 Provider 把「某平台的协议」翻译成「平台中立的模型」。

```python
class Provider(abc.ABC):
    platform: str                       # 平台标识
    capabilities: PlatformCapabilities  # 真实能力，必须如实申报

    def connect(self) -> None: ...      # 生命周期
    def disconnect(self) -> None: ...
    def poll(self, cursor, limit) -> PollResult: ...   # 增量拉取
    def send(self, outgoing) -> Message: ...            # 发送（模板方法）
    def recall(self, message_id) -> bool: ...          # 可选能力
    def history(self, ...) -> tuple[Message, ...]: ...  # 可选能力
```

三个不变量：

1. **能力闸门在 `Provider.send()` 内部强制生效**。`send()` 是模板方法：先 `ensure_can_send()` → `ensure_text_length()` → 才调用子类的 `_send()`。调用方即便直接调 `provider.send()` 也绕不过检查 —— 「不伪造能力」不能依赖上层自觉。
2. **游标语义由 Provider 自己定义**（希沃用页码，Satori 用 `sn`），上层只负责原样保存与回传。
3. **可选能力缺失时抛 `CapabilityError`，不静默忽略、不假装成功**。

`CallbackProvider` 是推送式平台的骨架：上游把事件推给 `on_message`，Provider 负责入队，`poll()` 从队列取走。这样推送式与拉取式平台在核心层看起来完全一样。

### 2.4 `core/` — 通用业务

`MessageService`（`core/service.py`）是**唯一的业务中枢**。它只认 Provider 契约，不认识任何平台。

| 关注点 | 实现 |
| --- | --- |
| **去重** | 双重：内存 LRU（`seen_capacity` 限制）+ Store 成员检查。LRU 防高频重复查询 Store，Store 兜住进程重启后重复 |
| **排序** | 复合键 `(timestamp, id)`。同一毫秒内按 id 兜底，保证全序且确定 |
| **断线恢复** | 游标经 Store 持久化。重连后从断点续拉；即使上游重投，靠去重保证不重复入库 |
| **并发** | `_ingest_lock` 串行化拉取入口；`store` 自身加锁 |
| **状态一致性** | `ConnectionState` 状态机 + 显式跃迁表，非法跃迁抛 `InvalidStateTransition` |
| **事件分发** | `_dispatch_lock` **串行化**派发，保证「事件顺序 == 消息顺序」 |
| **错误传播** | Provider 异常 → `ERROR` 事件 + 继续上抛，**不吞** |
| **认证自愈** | 认证失效时刷新凭证并**重试恰好一次**（安全，因为上游在鉴权阶段就失败、没写入业务数据），仍失效则抛 `SessionExpiredError` |

`Store`（`core/store.py`）是持久化抽象：`MemoryStore`（测试用）/ `JsonStore`（落盘）。`JsonStore` 沿用**旧 `chat_history.json` 格式**（`{"messages": [...]}`），可直接读取历史数据；写入用 `tempfile.mkstemp` + `os.replace` 原子替换，避免半截 JSON。

### 2.5 `providers/seewo.py` — 希沃适配

**整个项目里唯一允许出现希沃专有符号的地方**。

```text
希沃数字 type ──┐
resUrl       ──┼──→ Segment / Message（平台中立）
statusCode   ──┘
```

职责：

- 数字 type → `SegmentType`（2→图片、3→音频、4→视频、5/6→文件，其余→文本）
- 业务码 → Provider 异常（`-500/-505` → `ProviderAuthError`，其余非 200 → `ProviderBusinessError`）
- 游标 = **1-based 页码**（希沃的 `start` 是页码不是消息 id）
- 媒体发送 = 先上传拿 `downloadUrl`，再以对应 type 发送
- `describe()` 输出平台与会话描述，供 `/status` 类接口使用

**不做**的事：不引入未经验证的希沃业务语义，不猜测协议行为。协议能力缺口记录在 [`CAPABILITIES.md`](CAPABILITIES.md)。

### 2.6 `integrations/` — 第三方框架协议适配

`IntegrationServer` 提供**跨协议复用的基础设施**：

| 能力 | 说明 |
| --- | --- |
| `translate_event` / `build_payload` | 核心事件 → 协议载荷。`build_payload` 是钩子，协议需要额外包信封时覆写 |
| `broadcast` / `send_payload` | 向多个连接广播 |
| `backoff_delay` / `should_retry` | 重连退避策略 |
| `resume_cursor` | 断线续传游标 |
| `deliver` | 统一的投递入口 |

已实现的协议：

| 协议 | 位置 | 说明 |
| --- | --- | --- |
| **OneBot 11** | `integrations/onebot11/` | HTTP API `POST /onebot/v11/<action>` + 正向 WS `/onebot/v11/ws`。事件用 `post_type`/`message_type`/`sub_type`/`sender{}` 形状，消息段用 CQ 码风格 |
| **Satori** | `integrations/satori/` | WS `/v1/events`（`{platform}/{account}/satori/v1/...` 也支持）+ HTTP API `POST /satori/v1/<resource>.<method>`。信令 opcode：EVENT=0 / PING=1 / PONG=2 / IDENTIFY=3 / READY=4，事件元素用 `<img>` / `<audio>` / `<at>` / `<quote>` |

**关键取舍**：不按框架各自重写一遍消息处理、状态管理和业务逻辑。统一核心 + 轻量适配器 —— 协议差异只体现在「事件形状」和「API 动作」两处，其余全部复用。

### 2.7 `interfaces/` — 对外 HTTP 接口

`interfaces/rest.py` 提供 REST v2（挂载在 `/api/v2`）：

- **统一响应信封**：`{"ok": true, "data": ...}` / `{"ok": false, "error": {"code": ..., "message": ...}}`
- **统一错误码** `ApiErrorCode` 与 HTTP 状态码一一对应（`capability`→501、`upstream`→502、`timeout`→504）
- `map_exception()` 把异常族统一映射为错误码
- `openapi.yaml` 给出 OpenAPI 3.1 描述

`interfaces/` 只依赖 `core`，**不认识希沃**。

---

## 3. 关键数据流

### 3.1 入站（希沃 → 第三方框架）

```text
希沃服务端
   │  msg.msg.get(count, start=page)
   ▼
SeewoProvider.poll()        数字 type → Segment；业务码 → Provider 异常
   │  PollResult(messages, cursor=page)
   ▼
MessageService.poll_once()  取游标 → provider.poll → ingest → 存游标
   │
   ├─ ingest(): 去重 → 排序 → 落 Store → _dispatch
   │
   ▼
_dispatch()（串行，_dispatch_lock）
   │  Event(type=MESSAGE, message=...)
   ├──► OneBot11Adapter.translate_event() → {"post_type":"message", ...}
   │      → OneBot11Server.broadcast() → WS 客户端
   ├──► SatoriAdapter.build_payload() → {"op":0,"body":{...}}
   │      → SatoriServer → WS 客户端
   └──► REST v2 SSE（GET /api/v2/events）
```

### 3.2 出站（第三方框架 → 希沃）

```text
OneBot API "send_msg" / Satori "message.create" / REST v2 POST /messages
   │  平台消息 → Segment 序列
   ▼
MessageService.send()
   │  Provider.send()：能力闸门 → 长度闸门 → _send()
   │     见沃 type=1；媒体先上传拿 downloadUrl 再 type=2/3
   │     认证失效 → refresh_session() → 重试一次 → 仍失效抛 SessionExpiredError
   ▼
_dispatch(Event(MESSAGE_SENT))
```

---

## 4. 可靠性设计要点

| 问题 | 处理 |
| --- | --- |
| 重复投递 | 双层去重（LRU + Store）。Provider 侧允许 at-least-once（`FakeProvider` 用较旧游标会重投），核心层负责收敛 |
| 消息乱序 | 复合排序键 `(timestamp, id)` |
| 事件乱序 | `_dispatch_lock` 串行派发，事件序 == 消息序 |
| 断线 | 游标持久化 + 续拉；Satori 侧另有 `sn` 会话恢复 |
| 状态不一致 | 显式跃迁表 + `InvalidStateTransition` |
| 半截文件 | `JsonStore` 原子写；缺 `messages` 键抛 `CorruptStoreError`（**故意不吞**：吞掉会让一次追加覆盖全部历史） |
| 慢消费者 | SSE 队列满时丢最旧项，**绝不阻塞核心派发** |
| 处理器泄漏 | SSE 生成器 `finally` 里 `off_any()` 摘除监听器 |
| 凭据过期 | 刷新后重试一次，失败则 `SessionExpiredError`（不无限重试） |

---

## 5. 为什么复用既有希沃实现

`providers/seewo.py` 直接调用 `msg.msg` / `stu.stu` / `upload.upload_file`，而不是重写 m-campus 协议。理由：

1. **协议层已验证**：`api.py` 的 `pxencode`/`pxdecode` 编解码、200 字上限、页码语义等都是踩坑沉淀下来的，重写风险高于收益。
2. **改造范围可控**：本次目标是把**业务**与**协议**解耦，不是重做协议。协议实现被完整封装在 Provider 内部，对上层完全不可见。
3. **可替换性不受影响**：上层只依赖 `Provider` 契约。将来若要换协议实现，替换 `providers/seewo.py` 即可，核心层零改动。

---

## 6. 不变量清单（改代码时不要破坏）

1. `models.py` / `errors.py` 只依赖标准库
2. 依赖方向只能向下（`interfaces` → `integrations` → `core` → `provider` → `models`）
3. 只有 `providers/seewo.py` 可 import 希沃协议层
4. `Provider.send()` 是模板方法，能力闸门必须在这里生效
5. 平台没有的能力 → 申报 `False` + 抛 `CapabilityError`，**不伪造、不静默**
6. 事件派发必须串行，保证事件序 == 消息序
7. v1 `/api/*` 响应格式与行为**保持不变**（兼容性硬约束）
8. `chat_history.json` 旧格式继续可读

---

## 相关文档

- [`CAPABILITIES.md`](CAPABILITIES.md) —— 能力声明与**已知能力缺口**
- [`INTEGRATION.md`](INTEGRATION.md) —— 接入 OneBot 11 / Satori 的实操手册
- [`MIGRATION.md`](MIGRATION.md) —— v1 → v2 迁移指南
- [`../AGENTS.md`](../AGENTS.md) —— 给 AI 助手的完整项目上下文
