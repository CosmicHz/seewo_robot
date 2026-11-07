# 能力声明与已知能力缺口

本文是「**不伪造能力**」原则的落地清单。

核心约定：

> **平台具备的能力才申报具备；不具备的，调用时抛 `CapabilityError`，不静默降级、不假装成功。**

客户端可以随时通过 `GET /api/v2/capabilities`（或 `GET /api/v2/status` 的 `capabilities` 字段）拿到**当前平台真实的能力表**，据此决定 UI 该显示哪些按钮、该走哪条发送路径。

---

## 1. 能力表字段

`PlatformCapabilities`（`seewo/models.py`）：

| 字段 | 含义 |
| --- | --- |
| `send_text` | 能否发送纯文本 |
| `send_image` | 能否发送图片 |
| `send_audio` | 能否发送音频 |
| `send_video` | 能否发送视频 |
| `send_file` | 能否发送文件 |
| `send_mention` | 能否发送提及 |
| `send_quote` | 能否发送引用 |
| `max_text_length` | 单条文本最大长度；`0` 表示不限 |
| `max_media_bytes` | 单个媒体文件最大字节数；`0` 表示不限 |
| `supports_receipt` | 上游是否返回「已送达」确认 |
| `supports_recall` | 是否支持撤回 |
| `supports_history` | 是否支持拉取历史消息 |
| `supports_stream` | 是否支持流式 / 增量事件推送 |

判定用 `capabilities.supports_segment(seg)`，它按段类型查表；`CapabilityError.capability` 会带上**具体是哪个字段不满足**（如 `send_image`、`max_text_length`）。

---

## 2. 希沃平台的真实能力

`SeewoProvider` 申报如下：

| 能力 | 值 | 依据 |
| --- | --- | --- |
| `send_text` | `true` | `POST_KIDNOTE_V1_NOTE` + `type=1`，项目已在用 |
| `send_image` | 取决于是否配置上传器 | `type=2` 需先有 `downloadUrl`；未注入 `uploader` 时申报 `false` |
| `send_audio` | 取决于是否配置上传器 | `type=3`，同上 |
| `send_video` | `false` | `type=4` 在协议里存在，但本项目**未实现**「上传→发送」链路，不申报 |
| `send_file` | `false` | 同上 |
| `send_mention` | `false` | 希沃留言协议**无提及语义**，硬造一个 `@xxx` 只是文本 |
| `send_quote` | `false` | 希沃留言协议**无引用语义** |
| `max_text_length` | `199` | 服务端硬上限 200 字（超长返回 `statusCode=40000` / "留言内容不能超过200字符"），客户端留 1 字余量 |
| `supports_receipt` | `true` | 响应体 `statusCode` 即业务确认 |
| `supports_recall` | `false` | 协议层有 `DELETE_KIDNOTE_V1_NOTE`，但**本项目未开放**该能力 |
| `supports_history` | `false` | `Provider.history()`（平台侧回溯）未实现，如实申报。本地历史由核心层 Store 提供（`/api/v2/history`），不经 Provider |
| `supports_stream` | `false` | 希沃是**轮询式**，无推送通道 |
| 语音转文字（ASR） | 未提供 | 项目只把语音作为媒体（`SegmentType.AUDIO` + URL）传递，没有任何识别实现；见 §3.7 |

> `send_image` / `send_audio` 是**条件能力**：注入 `uploader` 后变为 `true`。这是有意的 —— 能力申报反映「当前这个 Provider 实例**实际**能做什么」，而不是「协议理论上支持什么」。
>
> **当前 `api_server` 没有给 v2 Provider 注入上传器**：上传器接受服务端本地文件路径，经 HTTP/协议端点暴露等于让已鉴权调用方把服务器上任意可读文件传到希沃云端。因此 v2 与 OneBot/Satori 下的媒体发送如实申报 `false`，调用返回 `501 capability`。**发媒体请走 v1 的 `/api/send_image` / `/api/send_audio`**。
>
> 即便注入上传器，以下组合也会被拒（协议行为未验证，不猜测）：文本+媒体混发、一条消息含多个媒体、媒体 URL 不是服务端本地文件路径。

---

## 3. 已知能力缺口

以下是**协议层本身不具备**（或本项目未实现）的能力。列在这里是为了避免下游误以为「接口漏了」，也避免有人为了「补齐」而伪造行为。

### 3.1 撤回（recall）

- **缺口**：协议层存在 `DELETE_KIDNOTE_V1_NOTE`，但本项目**未接入**，且删除语义（只能删自己发的？删了对方能看到吗？）**未经真机验证**。
- **当前行为**：`supports_recall = false`；调 `provider.recall()` 抛 `CapabilityError("supports_recall", ...)`。
- **要补齐需要的契约**：希沃删除接口的入参（留言 id？家长 uid？）、删除后对方客户端的表现、可撤销的时间窗。**这些都需要真机验证**，本次不猜测。

### 3.2 提及（mention）与引用（quote）

- **缺口**：希沃留言是**单家长 ↔ 单学生**的一对一通道，没有群组、没有成员列表、没有消息引用。
- **当前行为**：`send_mention = false`、`send_quote = false`。收到 `@` 文本时它只是**普通文本**，不会被解析成结构化提及。
- **要补齐需要的契约**：群组概念（成员 uid 列表、群 id）、提及/引用的服务端表示方式。希沃侧**很可能根本没有**这个概念 —— 那属于「平台不具备」，不是「待实现」。

### 3.3 视频与文件发送

- **缺口**：`type=4`（视频）/ `type=5`（文件）在协议里存在，但本项目未实现对应的「上传→发送」链路。
- **当前行为**：`send_video = false`、`send_file = false`。
- **要补齐需要的契约**：上传接口对视频/文件的大小与格式限制（`upload.py` 当前按扩展名推导 Content-Type，**未验证**服务端是否接受）、`voiceLength` 之外的时长字段是否需要。**需要真机验证**。

### 3.4 流式推送（stream）

- **缺口**：希沃无推送通道，只能轮询。
- **当前行为**：`supports_stream = false`。事件对外有两条路：轮询 `GET /api/v2/messages`，或订阅 SSE `GET /api/v2/events` —— 但 SSE 的数据源仍是**轮询**出来的，不是上游推送。
- **影响**：v2 没有后台轮询，事件延迟取决于外部调用 `/api/v2/sync` 或 `GET /api/v2/messages` 的频率；`base_interval` 只用于 v1 / 路径 A。

### 3.5 多会话 / 多学生 / 多家长

- **缺口**：希沃侧是「一个家长账号绑定若干学生」，但本项目的 `chat_history.json` / `tokens.json` 是**扁平存储**，没有按学生 UID 分命名空间。
- **当前行为**：会话 id 取学生 `userUid`（见 `SeewoProvider._conversation_id()`），但**落盘仍不隔离**。
- **要补齐需要的契约**：这属于**存储层**改造而非协议缺口。切换学生前需手动备份/清理数据文件。参见 [`../README.md`](../README.md)「设计限制」。

### 3.6 单条媒体大小上限

- **缺口**：`max_media_bytes` 目前恒为 `0`（不限），**未验证**希沃云存储的真实上限。
- **当前行为**：不拦截，靠上传接口报错。
- **要补齐需要的契约**：需真机确认服务端限制值。

---

## 4. 扩展能力时的怎么做

**要新增一项能力**，按这个顺序：

1. **先确认平台真的支持**。翻协议文档或真机验证；**不确定就别加**。
2. 在对应 Provider 的 `capabilities` 里如实申报。
3. 若涉及新的消息段类型（如 `SegmentType.FILE`），在 `models.py` 的 `SegmentType`、`PlatformCapabilities.supports_segment()`、`CAPABILITY_FIELD_BY_SEGMENT` 三处**同步**加（漏一处会导致 `CapabilityError.capability` 定位不到字段）。
4. 若涉及新的 API 动作（如 OneBot 的某个 action），在适配器的 `handle_api` / `_api_*` 里实现，并补测试。
5. **补测试**：正向（能力可用时行为正确）+ 反向（能力不可用时抛 `CapabilityError`，而不是静默成功或静默失败）。

**要新增一个平台**：实现 `Provider` 子类即可。核心层、接口层、集成层**零改动**。契约测试见 [`../tests/test_provider_contract.py`](../tests/test_provider_contract.py) —— 同一套断言会跑在 `FakeProvider` 与真实 Provider 上。

---

## 5. 相关文档

- [`ARCHITECTURE.md`](ARCHITECTURE.md) —— 分层与依赖方向
- [`INTEGRATION.md`](INTEGRATION.md) —— 框架接入实操
- [`../tests/test_provider_contract.py`](../tests/test_provider_contract.py) —— Provider 契约测试

### 3.7 语音转文字（ASR）

- **缺口**：入站语音（希沃 `type=3`）只被当作媒体段（`SegmentType.AUDIO` + `url` + `duration`）传递；项目没有 ASR 依赖或实现，全仓库检索 `asr` / `speech-to-text` / `transcript` / `whisper` / `语音识别` 均无结果。
- **影响**：基于文本的自动回复、命令处理与消息检索无法消费语音留言。
- **建议的补齐方式**（尚未实现，需维护者决策）：新增默认关闭的配置（如 `asr.enabled` / `asr.provider` / `asr.language`）；对入站音频段在 `Segment.data["transcript"]` 附带识别文本而**不伪造文本段**；识别失败只记日志、原音频段照常投递。若接入外部识别服务，应封装在 Provider 或独立适配层，不进入 `seewo/core`。
- **不猜测**：希沃协议本身是否已返回转写结果**未经证实**，不做假设。
