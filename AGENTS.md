# AGENT 开发指南

> 这是给 AI 助手的完整项目上下文。读完后应能直接接手开发，不需要再读源码就能动手改任意模块。

## 项目定位

希沃云班「亲情留言」聊天机器人。通过「希沃统一服务平台」API，以家长身份收发留言、执行命令、上传媒体、查询考勤。所有外部请求的目标服务器是希沃生产服务（`m-campus.seewo.com` / `campus.seewo.com` / `id.seewo.com`），可在 `config.json` 切到本地 mock 服务器调试。

**v2 架构目标**（2026-11 起）：在保留原有两条路径的前提下，建立一个**业务与协议充分解耦**、拥有稳定扩展接口、能接入主流聊天机器人生态的**通用消息服务**。核心在 `seewo/` 包，见 [分层架构](#通用消息服务架构seewo-包)。

## 快速开始（拿到代码后）

```bash
# 1. 安装依赖（推荐使用 uv）
# 用户
uv sync          # 或 pip install -e
# 贡献者
uv sync --extra dev          # 或 pip install -e ".[dev]"

# 2. 复制配置
cp config.json.example config.json   # PowerShell: Copy-Item

# 3. 首次登录（生成 tokens.json）—— 任一路径都需要
uv run python main.py            # 路径 A：终端扫码后开始监听
# 或
uv run python api_server.py      # 路径 B：启动服务后用客户端引导扫码

# 4. Mock 调试（不接触真实希沃服务器）
# config.json 设 use_mock=true，先启 mock_server.py，再启 api_server.py
uv run python mock_server.py &
uv run python api_server.py
uv run python tui_client.py

# 5. 测试
uv run python test/test_api.py    # 逐个测试所有 API 端点
```

**Windows 注意**：PowerShell 无 `cat`，commit message 用多个 `-m` 或临时文件；`&&`/`||` 不是分隔符，用 `;`。

### 依赖管理约定（硬性）

- 依赖的**唯一真源**是 `pyproject.toml`；新增/移除依赖只改这里，改了之后用 `uv sync` 或 `pip install -e ".[dev]"` 安装，**不通过 `requirements.txt` 管理依赖**。
- `requirements.txt` 是 **pip-compile 生成的锁文件（生成产物），不可手改、不作为依赖清单**；它只是给 pip 用户的可复现版本快照。文档一律引导用户用 `pyproject.toml` 装依赖。
- `uv.lock` **不跟踪**，由本机 `uv sync` 自动生成；不要把它当作跨环境版本依据。
- `pre-commit` 钩子（`.githooks/pre-commit`，激活：`git config core.hooksPath .githooks`）：**对所有暂存的 `.py` 执行 `ruff format` 并重新暂存**（幂等）；暂存了 `pyproject.toml` 时再用 `pip-compile` 自动增量更新 `requirements.txt` 并暂存（复用已有 pin，不整体重解析）。**若本机缺 `ruff` 或 `pip-tools` 则 `exit 1` 阻止提交**（勿 `--no-verify` 绕过）。
- [`.github/workflows/ci.yml`](.github/workflows/ci.yml) 在 PR 上用与 hook 完全一致的 `pip-compile` 重新生成并对比，拦截任何绕过本机 hook 的锁文件不同步提交；CI 同时跑 `ruff check` / `ruff format --check` / `pytest`，lint 规则集见 [pyproject.toml](pyproject.toml) `[tool.ruff.lint]`（BLE001 暂全局忽略，见已知问题）。

## 两条独立运行路径（最高优先级约束）

项目存在**两条代码路径互相独立**的运行路径：**内存中的会话对象各自维护**（路径 A 全局 `account/student/stu_msg`；路径 B `Session` 对象），但**共享磁盘上的状态文件**（`tokens.json` / `chat_history.json` / `config.json` / `uploads.json` / `logs/`）。任一路径扫码后写回的 `tokens.json` 可被另一路径直接读取复用，**只有凭证不存在或失效时才需扫码**。

### 路径 A：独立脚本（直连希沃）

| 脚本 | 职责 |
| --- | --- |
| [main.py](main.py) | 常驻轮询监听最新留言 + 处理 `/` 开头命令 + 自适应轮询间隔 + 断线重连 |
| [send_msg.py](send_msg.py) | 一次性登录并发送一条文本 |
| [upload_file.py](upload_file.py) | 一次性登录并上传文件至希沃云存储接口 |
| [yunban_token.py](yunban_token.py) | 动态构建生产云班 `yunban` 实例（全局名 `yunban_client`）：不硬编码敏感信息，从现有登录会话推导 `acw_tc` cookie + schoolUid。其他 yunban 脚本依赖它 |
| [auto_attend.py](auto_attend.py) | 自动签到（**粗糙，非生产级**：硬编码 `classlist[35]`，事件用 `events[1]` 无兜底会崩溃；已加 `__main__` 保护，其余运行时语义与 owner 原版一致） |

### 云班工具脚本（复用 `yunban_token.yunban_client`，直连云班 REST）

| 脚本 | 职责 |
| --- | --- |
| [yunban_cli.py](yunban_cli.py) | 云班命令行封装：`classes` / `student` / `events` / `eventtime` / `attend`（考勤）/ `roster` |
| [yunban_tui.py](yunban_tui.py) | 基于 Textual 的云班 TUI：三列（班级 / 学生+事件 / 上一接口原始响应）+ 家长列表，选中出详情与操作按钮，接口结果自动持久化缓存并用 `r` 手动失效 |
| [yunban_token.py](yunban_token.py) | 同上，动态构建 `yunban` 实例供上述脚本复用 |

> 云班脚本需防风控：yunban.py 绕过 `request_manager`，一次成批取数/签到前务必自行 `time.sleep` 间隔。

### 路径 B：客户端-服务端（HTTP 中转）

| 文件 | 职责 |
| --- | --- |
| [api_server.py](api_server.py) | REST API 服务端，自身维护 `Session`（`auto_login=False`：过期返回 `need_login=true` 由客户端引导扫码，不阻塞服务） |
| [tui_client.py](tui_client.py) | 基于 Textual 的 TUI 客户端 |
| [client.py](client.py) | CLI 客户端 + Python SDK |

> 路径 B 的客户端**只**经 HTTP 与 `api_server.py` 通信，本身不登录、不接触希沃；`api_server` 不在线时无法使用。

### 路径功能差异

- 路径 A 独有：自适应轮询、断线重连
- 路径 B 独有：图片发送、按需历史加载、全量同步、异步扫码登录、状态查询、长消息 split + 智能拆分点
- 推荐通过路径 B 进行使用和开发

## 项目分层架构（仅路径 B 的现状）

```text
入口层 (api_server.py handler)        ← 薄层：参数校验、调用 datasource、包装响应
   │
   ▼
数据源层 (message_service.py)        ← 独占 chat_history.json 读写 + 格式化 + 多页聚合
   │   ├── 内存缓存（基于文件 mtime 感知外部写入）
   │   └── sync_all 的翻页循环在本层
   │
   ▼
数据访问层 (msg.py / stu.py / upload.py / yunban.py)  ← 单次 DAO，纯网络接口封装
   │   └── msg.py 已删除多页聚合，只做单页请求
   │
   ▼
API 网关 (api.py)                    ← m-campus 统一接口，pxencode/pxdecode 编解码
   │
   ▼
统一请求层 (request_manager.py)      ← 节流 + 429 退避 + 队列化预留
   │
   ▼
数据模型层 (models.py)                ← 进程内 dataclass 模型（RawMessage / Message / MessageResponse）
   │
   ▼
基础设施 (init.py / login.py / funcs.py)  ← 配置、登录、文件读写工具
```

> 上图是**路径 B 的 v1 现状**。v2 的通用消息服务内核在 `seewo/` 包，是**并行的另一套**（v1 端点行为不变）。见下节。

## 通用消息服务架构（`seewo/` 包）

### 分层与依赖方向

依赖**严格单向向下**，上层可调下层，下层**不得**反向 import 上层：

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

**唯一例外**：`seewo/providers/seewo.py` 被允许 import 仓库根的希沃协议层（`api`/`login`/`msg`/`stu`/`upload`）—— 复用既有协议实现，**刻意选择，非破例**。

### 关键不变量（改代码时不要破坏）

1. `models.py` / `errors.py` **只依赖标准库**
2. 依赖方向只能向下（`interfaces` → `integrations` → `core` → `provider` → `models`）
3. 只有 `providers/seewo.py` 可 import 希沃协议层
4. **`Provider.send()` 是模板方法**：能力闸门（`ensure_can_send` + `ensure_text_length`）在里面强制生效，子类只实现 `_send()`。**不要覆写 `send()`** —— 那会绕过闸门
5. 平台没有的能力 → `capabilities` 申报 `False` + 抛 `CapabilityError`，**不伪造、不静默**
6. 事件派发必须串行（`_dispatch_lock`），保证事件序 == 消息序
7. v1 `/api/*` 响应格式与行为**保持不变**
8. v2 **不读写** v1 的 `chat_history.json`：v2 用独立的 `chat_history.v2.json`，`JsonStore` 遇到 v1 结构拒绝加载（防覆盖）

### 架构守卫（自动化）

`tests/test_layering.py` 用 AST 解析 import，把上述约定变成**可执行的检查**：依赖方向、平台专有模块隔离、无逆向依赖、无 TODO 残留、文件数 ≤ 30（防过度设计）。**改依赖结构时这个文件会告诉你哪里越界了。**

### 各模块职责（`seewo/`）

| 模块 | 职责 | 关键点 |
| --- | --- | --- |
| `models.py` | 平台中立模型 | `Segment`（`type` 判别 + 扁平字段）、`Message`（`sort_key` = `(timestamp, id)`）、`User`/`Conversation`/`Event`/`PlatformCapabilities`/`ConnectionState`（显式跃迁表）。平台细节只能进 `Segment.data` / `Message.native` |
| `errors.py` | 异常体系 | 平台错误码在 Provider 边界被翻译成中立语义（认证失效/业务拒绝/连接失败/超时）。`CapabilityError.capability` 指出具体是哪个能力字段不满足 |
| `provider/base.py` | Provider 契约 | 平台接入的**唯一**扩展点。`poll(cursor, limit)` 游标语义由 Provider 自己定义（希沃无增量游标，返回 `None`）。`CallbackProvider` 供推送式平台 |
| `core/store.py` | 持久化 | `Store` ABC / `MemoryStore` / `JsonStore`。`JsonStore` 存 v2 记录、容器仍是 `{"messages":[...]}`；所有读写加锁；原子写（`mkstemp` + `os.replace`）；缺 `messages` 键抛 `CorruptStoreError`，遇到缺 `segments` 的 v1 结构同样拒绝加载（**故意不吞**） |
| `core/service.py` | 通用业务中枢 | 双层去重（LRU + Store）、复合排序键、游标持久化、`_ingest_lock` 串行化拉取、`_dispatch_lock` 串行化派发、认证自愈（刷新后**重试恰好一次**） |
| `providers/seewo.py` | 希沃适配 | **唯一**允许出现希沃专有符号的地方。数字 type → `SegmentType`；业务码 → Provider 异常；每次 poll 固定拉第 1 页（不提供游标）；媒体只接受服务端本地文件路径，文本+媒体 / 多媒体混发抛 `CapabilityError`；发送回执 id 为空串（希沃不返回服务端 id） |
| `integrations/base.py` | 集成基础设施 | `translate_event` / `build_payload`（钩子）/ `broadcast` / `send_payload` / `backoff_delay` / `resume_cursor` / `deliver` |
| `integrations/onebot11/` | OneBot 11 | HTTP `POST /onebot/v11/<action>` + 正向 WS `/onebot/v11/ws`。retcode：OK=0 / UNSUPPORTED=100 / ILLEGAL_PARAM=1400 / AUTH=1401 / FORBIDDEN=1403；字符串消息按 CQ 码解析（`auto_escape=true` 除外） |
| `integrations/satori/` | Satori | WS `/v1/events` + HTTP `POST /satori/v1/<resource>.<method>`。opcode：EVENT=0 / PING=1 / PONG=2 / IDENTIFY=3 / READY=4 |
| `interfaces/rest.py` | REST v2 | 统一信封 `{"status","data"|"error"}`；`ApiErrorCode` ↔ HTTP 状态码一一对应（capability→**501**、upstream→502、timeout→504）；`map_exception()` 是错误契约的唯一入口 |
| `interfaces/openapi.yaml` | 接口描述 | OpenAPI 3.1 |
| `sdk.py` | 类型化 SDK | `SeewoRobotClient` + `SeewoApiError` + `SendResult`。**只收 URL，不代劳上传** |
| `testing/fake_provider.py` | 测试替身 | `FakeProvider` 实现了完整 Provider 契约，**at-least-once** 游标语义（用旧游标会重投），可跑全部离线测试 |

### 扩展指引

**接新协议** → 实现 `IntegrationServer` 子类（`translate_event` + `send_payload`），加一个 Server 注册 HTTP/WS 路由。免费获得广播、退避、续传、事件订阅。

**接新平台** → 实现 `Provider` 子类（`poll` + `_send` + `capabilities`）。核心层、接口层、集成层**零改动**。

**加新能力** → 先确认平台**真的支持**（翻文档或真机验证），再改 `Provider.capabilities`；涉及新段类型时 `models.py` 三处要同步（`SegmentType` / `supports_segment()` / `CAPABILITY_FIELD_BY_SEGMENT`）。

**协议细节** → 只在 `providers/seewo.py` 改；上层不认识 `resUrl` / 数字 type / `statusCode`。

### 各层职责（关键）

- **[request_manager.py](request_manager.py)**：所有对希沃的 HTTP 请求**必须**经此调度。当前实现 `_throttle()` 全局节流（`MIN_INTERVAL=0.5s`，`threading.Lock` 串行化节流点）+ HTTP 429 指数退避重试（3 次）。预留队列化接口（`queue.Queue + worker`，当前同步执行）。**新增任何对希沃的 `requests.post` 都必须改用 `request_manager.post`**。
- **[api.py](api.py)**：m-campus API 调用网关。`api().action(type, params, account)` 把 pxencode 后的参数 POST 到 `/class/apis.json?action=<type>`，返回响应 JSON。内部已用 `request_manager.post`。
- **[msg.py](msg.py)**：纯 DAO。只做单次请求，**不再有**多页聚合逻辑。`get(count, start=1) -> MessageResponse` 是核心方法，`start` 是 1-based 页码（start=1=最新一页，递增往更旧翻页，页内按 id 升序，页间无重叠）；调用方通过 `.result` 取 `tuple[RawMessage, ...]`。
- **[message_service.py](message_service.py)**：消息数据源层。`MessageDataSource` 独占 `chat_history.json` 读写、消息格式化、内存缓存（基于文件 mtime + 大小感知 `main.py` 等外部写入）。公开接口：`fetch_latest()` / `load_local()` / `local_bounds()` / `persist(raw_messages)`；`_format_msg`、`_messages` 为内部实现，**外部与测试都不应触碰**。对外方法返回 dict（边界 `dataclasses.asdict` 转 dict 给 jsonify）。`sync_all` 翻页循环在本层。
- **[models.py](models.py)**：数据模型层。进程内数据传输用 dataclass，跨进程边界（Flask jsonify / 写 chat_history.json / pxencode 网络请求）调 `dataclasses.asdict()` 转换。含 `Config` 配置模型（可变，供 `reload_config` 原地更新实现热重载）。详见 [数据模型层（models.py）](#数据模型层modelspy)。
- **[api_server.py](api_server.py)**：入口层，handler 薄层化。全局 `session = Session()` + 全局 `datasource = MessageDataSource(session)`（构造时仅 `_refresh` 读 mtime，不碰 session）。
- **[init.py](init.py)**：全局配置 `config`（`models.Config` dataclass，可变）、`reload_config()`（重读 config.json 原地更新以支持热重载）、文件路径、`_use_mock`、公共请求头 `headers_nocookie`、希沃 URL 集合 `urls`、统一本地时区 `LOCAL_TZ`（北京时间；所有业务时间格式化/考勤取数用它，勿用系统本地时区）。所有状态文件路径（`config.json`/`tokens.json`/`uploads.json`/`qrcode.png`）由 `project_path()` 基于 `__file__` 锚定到项目根目录，不依赖进程当前工作目录。
- **[login.py](login.py)**：`acc` 账户对象 + `download_qrcode` / `check_qrcode` / `login` 流程。`acc(auto_login=True/False)` 控制过期时是否自动触发扫码：`main.py` 用 True，`api_server.py` 用 False。
- **[stu.py](stu.py)**：学生信息 DAO。不传 `child_uid` 时默认取关联列表第一个学生；按 `child_uid`（userUid）稳定标识选取，**生产代码请用 `child_uid`**（列表顺序不受服务端保证）；`count`（按下标选取）仍保留但已不推荐，与 `child_uid` 同时传入时 `child_uid` 优先并警告
- **[funcs.py](funcs.py)**：工具函数。`chat_log_file()` 每次调用即时按 `config.use_mock` 返回 `chat_history_mock.json` / `chat_history.json` 路径（**不要缓存成模块常量**，配置读后即过期）。`load_chat_history() -> list[Message]`、`append_message` / `merge_messages(messages: list[Message])` 只维护 `messages` 字段，不维护 `earliest_id` / `last_id`；`merge_messages` **不去重**（同 id 重复传入会重复落盘，调用方自行保证）。
- **[upload.py](upload.py)**：文件上传的唯一实现 `upload_file(account, file, type=None) -> downloadUrl`（`main.py` / `upload_file.py` / `api_server.py` 都调它，不要各自重复封装）。`type=None` 时按扩展名用 stdlib `mimetypes` 推导 Content-Type（显式传入的值优先）；上传失败时 `downloadUrl` 保持空串、失败原因在 `error`。
- **[yunban.py](yunban.py)**：云班功能扩展（班级列表、考勤事件、签到等）。多数方法走独立的 campus 云班 REST 端点（`/api/classmember` / `kidnote` / `attendance`），**直接 `requests.request`，绕过 `request_manager`**（无统一节流，调用方需自行控制频率）。各 get 函数返回数据模型（`YunbanClass` 等）。`getpass` 获取离线验证码、`getpass2` 监听剪贴板（依赖 `pyperclip`）。
- **[qrcode.py](qrcode.py)**：终端二维码渲染（依赖 `pillow`）。

## 核心 API 端点（路径 B）

所有 `/api/*` 接口需 `X-API-Key` header 认证（`config.json:api_key`）。端口默认 5001（`config.json:api_port`）。

| 端点 | 方法 | 数据源层方法 | 说明 |
| --- | --- | --- | --- |
| `/api/status` | GET | — | 服务状态 + 学生信息 |
| `/api/messages` | GET | `datasource.fetch_latest(count)` | 实时向希沃取最新一页，**不持久化** |
| `/api/send` | POST | `_send()` → `session.stu_msg.send` | 文本发送，支持长消息 `strategy`；响应带 `seewoCode`（希沃业务码）；`-500/-505` 自动刷新会话后重发一次，仍失效则 401 `need_login` |
| `/api/send_image` | POST | `upload_file_to_cloud` + `_send()` | 图片发送；发送失败返回 500（不再一律回 ok） |
| `/api/send_audio` | POST | `upload_file_to_cloud` + `_send()` | 音频发送（上传成功后只发送一条音频留言） |
| `/api/history` | GET | `datasource.load_local(offset, limit)` | 读本地缓存，分页 |
| `/api/load_earlier` | GET | `datasource.load_earlier_from_local(before_id, count)` | 纯本地读更早消息（不请求希沃，靠 `sync_all` 提前同步） |
| `/api/sync_all` | POST | `datasource.sync_all(batch_size, delay)` | 全量同步历史到本地（防风控） |
| `/api/refresh` | POST | `session.refresh()` | 重新初始化会话 |
| `/api/config/reload` | POST | `init.reload_config()` | 热重载 config.json：原地更新全局 config + 重应用日志级别（API Key/长消息策略/拆分正则/mock 路由即时生效） |
| `/api/login/qrcode` | GET | `download_qrcode` + 后台 `_poll_login` 线程 | 获取登录二维码（Base64），同时启动后台轮询（不阻塞服务） |
| `/api/login/status` | GET | — | 查询扫码状态（`idle` / `pending` / `ok` / `error`） |

> 路径 B **不提供命令执行接口**：原 `/api/execute` 已移除（任意命令执行不再经 HTTP 暴露）。以 `/` 开头的留言仅在路径 A（`main.py`）里被解释为命令。

## v2 与协议端点（与 v1 并存）

v1 的 `/api/*` **行为不变**；下表全部是**新增**端点，与 v1 共用同一个 `MessageService` 实例与同一个端口。

| 端点 | 方法 | 说明 |
| --- | --- | --- |
| `/api/v2/status` | GET | 平台、连接状态、**能力表**、会话信息 |
| `/api/v2/capabilities` | GET | 平台能力声明（`PlatformCapabilities.to_dict()`） |
| `/api/v2/messages` | GET | 实时拉取（`count` 1–200，越界 400）。走核心层 → **写入 `chat_history.v2.json` 并去重** |
| `/api/v2/messages` | POST | 发送。body `{"segments": [...]}` 或简写 `{"content": "..."}` |
| `/api/v2/history` | GET | 本地历史（`limit` ≤ 500 / `offset`），返回 `total` |
| `/api/v2/sync` | POST | 触发一次拉取并接收，返回 `count` |
| `/api/v2/events` | GET | **SSE 事件流**（平台中立） |
| `/api/v2/connection` | POST | `{"action": "connect"\|"disconnect"}` |
| `/onebot/v11/<action>` | POST | OneBot 11 HTTP API |
| `/onebot/v11/get_status` | GET | OneBot 11 状态 |
| `/onebot/v11/ws` | GET | OneBot 11 正向 WebSocket |
| `/satori/v1/<action>` | POST | Satori HTTP API（`{resource}.{method}`） |
| `/<platform>/<account>/satori/v1/<action>` | POST | Satori HTTP API（带前缀形式） |
| `/v1/events` | GET | Satori 事件 WebSocket |

v2 响应信封与错误码见 [docs/MIGRATION.md](docs/MIGRATION.md)；协议细节见 [docs/INTEGRATION.md](docs/INTEGRATION.md)。

> **挂载位置**：`api_server.py` 末尾用 `flask_sock.Sock(app)` 把两个 WS 端点挂到**同一个** Flask app（flask-sock 以蓝图方式注册路由），因此 HTTP 与 WS 共用 `api_port`。`tests/test_protocol_endpoints.py` 校验所有端点真的挂上了 —— 防「适配器写好了但 WS 端点从未 attach」这类回归。

## 核心数据约定

### 消息类型（`type` 字段）

| 值 | 含义 | 必需字段 |
| --- | --- | --- |
| 0 / 1 | 文本 | `content` |
| 2 | 图片 | `resUrl` |
| 3 | 音频 | `resUrl` + `voiceLength` |
| 4 | 视频 | `resUrl` |
| 5 | 文件 | `resUrl` |
| 6 | 富媒体 | `resUrl` + `resConfig` |

### 消息顺序约定

- **所有消息列表顺序稳定为旧→新**（按 `id` 升序）。
- `msg.get` 返回的页内顺序**不**假设稳定，`message_service` 内部按 `id` 排序后再返回。
- 客户端显示必须按返回顺序（旧在上、新在下）。

### earliest_id / last_id

- 调用方（`main.py` / `api_server.py` / `tui_client.py`）**需要时自行从 messages 推断**：

  ```python
  last_id = max(m.id for m in messages) if messages else 0  # 最新
  earliest_id = min(m.id for m in messages) if messages else 0  # 最旧
  ```

- 服务器响应体**不包含** earliest_id / last_id 字段，客户端自行从 messages 推断。

### 数据模型层（models.py）

进程内数据传输使用 dataclass 模型，跨进程边界（Flask jsonify / 写 chat_history.json / pxencode 网络请求）调 `dataclasses.asdict()` 转 dict。只建模跨多模块频繁传输的核心数据，一次性构造+消费的 dict（DAO 请求参数、handler 响应体、upload/stu/yunban 内部响应）保留 dict 不建模。

| 模型 | frozen | 用途 |
| --- | --- | --- |
| `RawMessage` | ✅ frozen+slots | 希沃原始消息（`msg.get().result` 列表元素），只读，字段缺失用默认值兜底（`senderType` 默认 `"unknown"`、`type` 默认 `1` 等，对齐原 `dict.get(key, default)` 行为） |
| `Message` | ❌ 只 slots，**可变** | 格式化后消息（`chat_history.json` 存储 / `message_service._messages` 缓存）。可变是为方便 `load_local` 补 `senderName`（直接 `m.senderName = name`，不必 `replace`）。`type` 标注 `int \| str`：保留 main.py 写`str(msg_type)` / message_service 写 int 的既有混合行为 |
| `MessageResponse` | ✅ frozen+slots | `msg.get` 的响应包装（含 `result: tuple[RawMessage, ...]`）。容器与 `result` 均真正不可变，需要增删/排序时消费方自行复制 |
| `SendResult` | ✅ frozen+slots | 发送类操作结果（`msg.send` / `yunban.send_msg`）：`ok` / `seewo_code`（希沃业务码）/ `http_status`（HTTP 码）/ `message`，两套码分开存放。`__bool__` 委托 `ok`，故既有 `if result:` 写法仍正确；`needs_relogin` / `is_business_reject` 谓词封装语义 |
| `Config` | ❌ 只 slots，**可变** | `config.json` 数据模型。可变是为了让 `init.reload_config()` 原地更新实现热重载。typed 字段覆盖已知配置项，未建模的原始键收集到 `extra` 保留，`banPaiConfig` 映射为 `ban_pai_config` dict 字段 |
| `YunbanClass` | ✅ frozen+slots | 云班班级（`getclasslist` 返回元素，含 uid/name/roomUid/schoolUid 等） |
| `YunbanStudent` | ✅ frozen+slots | 云班学生（`getstulist` 返回元素，含 name/sid/uid/gender/卡号等） |
| `YunbanEvent` | ✅ frozen+slots | 云班考勤事件（`getevents` 返回元素，含 name/eventId/起止时间/周期/绑班/config 班牌时段等） |
| `YunbanParent` | ✅ frozen+slots | 云班家长（`getparents` 返回元素，含 parentName/手机号/bindWx/未读数/关系位） |
| `YunbanNote` | ✅ frozen+slots | 云班留言（`getnotes` 的 `result` 元素，含 sender/receiver/type/status/resUrl/时间等） |
| `YunbanNotesPage` | ✅ frozen+slots | 云班留言分页（`getnotes` 返回结构，`result: tuple[YunbanNote, ...]`） |

> 云班模型（`Yunban*`）均沿用 `from_dict` + `.get` 兜底约定（frozen+slots），未建模的原始键收集到 `extra` 保留。

**核心改造点**：

- `funcs.load_chat_history() -> list[Message]`、`append_message` / `merge_messages` 内部构造 Message 实例
- `msg.get(count, start) -> MessageResponse`、辅助方法 `get_id` / `get_all_ids` / `get_content_by_index` / `get_msg_detail` 用属性访问（`.result` / `m.id` / `m.content`）
- `message_service._format_msg(raw: RawMessage) -> Message`（内部）、`_messages: list[Message]`（内部缓存）、公开接口 `fetch_latest()` / `load_local()` / `local_bounds()` / `persist(raw_messages)`、对外方法返回 dict（边界 `dataclasses.asdict(m)` 转 dict 给 jsonify）
- `main.py` 字段访问从 `.get("x", default)` 改为 `.x`（运行时行为不变，AGENTS.md 硬约束已放宽至此）
- `yunban.py` 各 get 函数返回云班模型（`Yunban*`），`searchstubyname`/`searchstubyuid` 未命中返回 `None`（原为 `{}`）

### 长消息处理（仅 `/api/send`，路径 B 独有）

希沃服务器对单条留言**强制 200 字硬上限**，超长返回 `statusCode=40000`。

| 策略 | 行为 |
| --- | --- |
| `truncate`（默认） | 截断为 196 字 + `...`（共 199 字） |
| `split` | 拆分为多条依次发送 |

- 全局配置：`config.json:long_message_strategy`
- 单次覆盖：`/api/send` body 加 `"strategy": "truncate" | "split"`，缺省取全局
- `split` 模式智能拆分点：`config.json:long_message_split_pattern`（Python `re` 正则，默认 `\r?\n`）。在前 199 字符范围内取最后一个匹配点，在匹配**之前**切割，匹配字符（如换行符）整体跟到下一段开头不丢失。无匹配或唯一匹配落在段首会致空段时回退硬切 199 字。设为空字符串 `""` 禁用智能拆分。
  - **JSON 反斜杠双写**：正则 `\r?\n` 在 JSON 中写为 `"\\r?\\n"`。

## 希沃 API 协议细节

### m-campus 统一接口模式

所有 m-campus API 走同一个 URL：`/class/apis.json?action=<type>`，靠 `action` 参数区分接口。请求体固定结构：

```python
encode_data = {"action": type, "params": pxencode(params)}
# pxencode = {"pxSafeData": f"scData:{base64(json(params))}"}
```

响应体是 `{"data": "scData:<base64>", "statusCode": ..., ...}`，`funcs.pxdecode(resp)` 取 `data[7:]` 后 base64 解码得到真实 JSON。

### 状态码约定（三套码，禁止混用）

`models.py` 是状态码语义的**唯一约定处**，不要在其它模块重复写字面量：

| 家族 | 定义处 | 取值 | 用途 |
| --- | --- | --- | --- |
| 希沃业务码 | `models.SeewoCode` | `OK=200` / `TOKEN_INVALID=-500` / `TOKEN_EXPIRED=-505` / `BUSINESS_REJECTED=40000` / `SERVER_ERROR=50000` | 响应体 `statusCode` 字段，表示业务结果 |
| 扫码登录码 | `models.SeewoQrCode` | `WAITING=200` / `SCANNED=201` / `CONFIRMED=202` | 仅 `pcCheckQrcode` 的扫码进度 |
| HTTP 状态码 | `requests` 的 `response.status_code` | 200/400/429/502… | 仅表示传输层结果 |

判定一律走谓词，不要自己比较数字：`SeewoCode.is_ok(code)` / `SeewoCode.is_relogin_code(code)` / `SeewoCode.is_business_reject(code)`；`SendResult` 上的 `needs_relogin` / `is_business_reject` 属性同理。

各码处理约定：

| code | 含义 | 处理 |
| --- | --- | --- |
| 200 | 成功 | 正常返回 |
| -500 / -505 | Token 无效 / 过期 | 刷新会话后重发**一次**（服务端未写入，重发安全）；仍失效 → 401 `need_login` |
| 40000 | 业务校验失败（如留言超 200 字） | 不重试，把原因返回给调用方 |
| 50000 | 服务器内部错误（如 SQL 错误） | 检查请求参数（如 `start<1` 触发） |

**两套码不得互相赋值**：`SendResult` 里 `seewo_code` 与 `http_status` 分开存放，
形如 `body.get("statusCode", response.status_code)` 的写法会把 502（HTTP）写成业务码，已被禁止。

### 关键 action 名速查

| action | 用途 |
| --- | --- |
| `GET_STUDENT_V1_PARENT_BYPARENTID_CHILDREN_LIST` | 获取学生列表 |
| `GET_KIDNOTE_V1_BYPARENTUID_BYCHILDUID_NOTES` | 获取留言（按 start 分页） |
| `POST_KIDNOTE_V1_NOTE` | 发送留言 |
| `DELETE_KIDNOTE_V1_NOTE` | 删除留言 |

### 登录状态机

```text
            download_qrcode() → cookies
                    │
                    ▼
         ┌─ check_qrcode(cookies)
         │
   status=200 ──→ status=201 ──→ status=202(成功，写 tokens.json)
   (待扫码)        (已扫码待确认)      │
         │              │             ▼
         └──────────────┴──→ 其他 status: 失败
```

`acc` 类的 Token 校验：调 `/mobile/user/v1/<uid>/functionality`，statusCode 200=有效，-500/-505=失效。

### main.py 轮询状态机

```text
   加载本地 chat_history → msg_id = max(messages.id)
            │
            ▼
   ┌──→ sleep(current_interval)
   │        │
   │        ▼
   │   stu_msg.get(POLL_BATCH_SIZE)
   │        │
   │   错误? ──→ consecutive_errors++ → ≥MAX_ERRORS? ──→ reconnect() (重新扫码)
   │     │                                          │
   │     │ 否                                       └──→ continue
   │     ▼
   │   new_ids = [id > msg_id]
   │        │
   │   空? ──→ current_interval = min(current_interval + 0.5, MAX_INTERVAL)
   │     │
   │     │ 否 ──→ current_interval = BASE_INTERVAL
   │     │          │
   │     │          ▼
   │     │   逐条 reversed(new_ids)（旧→新）：append_message + handle_command
   │     │          │
   └─────┴──────────┘
```

## Mock 模式（跨两条路径）

`config.json:use_mock=true` 时，所有请求指向 `http://localhost:{mock_port}`。

- **mock 服务器**：[mock_server.py](mock_server.py) 模拟希沃的所有接口（登录、用户状态、m-campus API、云班 API、COS 上传），并提供管理接口：
  - `POST /mock/add_message` — 注入测试消息
  - `GET /mock/data` — 查看所有 mock 数据
  - `POST /mock/reset` — 重置为初始状态
  - `POST /mock/save` / `POST /mock/load` — 持久化到 `mock_data.json` / 加载
  - `POST /mock/clear_messages` — 清空消息
- **mock 与生产对齐的关键约束**：
  - 单条留言 200 字上限（超长返回 `statusCode=40000`）
  - `start` 是 1-based 页码，`start<1` 触发 SQL 语法错误（`statusCode=50000`，对齐生产 MyBatis 行为）
  - 页内按 id 升序（旧→新），页间无重叠
- **云班 REST 对齐已统一**：生产实测云班同一模块跨 v1~v5 版本均存活（版本号不敏感），mock 用 `<int:version>` 通配；`classmember`/`kidnote`/`attendance` 各端点返回结构已按新模型对齐（`kidnote` 留言返回分页 `{page,pageSize,result,totalCount}`、家长计数返回 `YunbanParent` 列表、事件用 `name` 字段、班级含 `schoolUid/schoolName/schoolType`）。内置孩子的家长表存于 `mock_data.parents`（按 child_uid 索引）。
- **聊天记录隔离**：mock 模式下 `funcs.chat_log_file()` 自动切到 `chat_history_mock.json`（每次调用即时读 `config.use_mock`），避免测试数据污染真实 `chat_history.json`。
- **UID 自动适配**：mock 遇到未知家长 UID 时自动接管 `mock_parent_001` 的身份和消息，无需手动配置。

## 开发模式（典型代码模板）

### 新增一个希沃 API 调用

1. 在合适的 DAO 文件（`msg.py` / `stu.py` / `yunban.py` / `upload.py`）加方法：

   ```python
   def some_action(self, ...):
       data = {"parentUid": self.acc.uid, "childUid": self.stu.userUid, ...}
       return json.loads(
           pxdecode(
               api().action("SOME_ACTION_NAME", data, self.acc)
           )
       )
   ```

2. **不要**用 `requests.post` 直接调用——必须经 `api().action`（已用 `request_manager.post`，自动节流 + 429 退避）。
3. mock_server.py 同步加对应 endpoint 模拟响应。
4. 如果是路径 B 的接口，加 datasource 方法 + api_server handler。

### 新增路径 B 的 REST 端点

```python
@app.route("/api/xxx", methods=["POST"])
@require_api_key
def xxx():
    err = _check_session()  # 1. 会话检查（必须）
    if err:
        return err
    try:
        data = request.get_json() or {}
        # 2. 参数校验
        if not data.get("required_field"):
            return jsonify({"status": "error", "message": "..."}), 400
        # 3. 调 datasource（数据相关）或 session.stu_msg（发送相关）
        result = datasource.some_method(...)
        # 4. 包装响应
        return jsonify(result)
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500
```

### 新增消息类型

1. `msg.py:send` 的 `match type` 加分支（设字段）
2. `message_service.py:_format_msg` 不需改（透传 `type` / `resUrl`）
3. 客户端显示层（`tui_client.py` / `main.py`）按 type 分支渲染

### 修改 DAO（msg.py / stu.py）

- **保持 `msg.get` 签名不变**（`get(count, start=1)`，start 是 1-based 页码）—— main.py 依赖此语义。
- 新增方法只做单次请求，**不要**在 DAO 加多页聚合——聚合逻辑在 `message_service.py`。
- 用 `api().action` 而非 `requests.post`。

### 修改统一请求层（request_manager.py）

- 改 `MIN_INTERVAL` 全局影响所有请求节流。
- 队列化预留：注释里的 `queue.Queue + worker` 是后续启用方向，调用方接口不变。

## 测试与调试

### 测试脚本

- [test/test_api.py](test/test_api.py)：逐个测试所有 API 端点，发送/图片/音频会跳过避免误发。

### 自动化用例（`tests/`，全程离线）

用 `uv run pytest`。用例分三层：

| 文件 | 覆盖 |
| --- | --- |
| `test_api_*.py` / `test_upload.py` / `test_mock_*.py` 等 | v1 既有端点（**回归基线**，勿改语义） |
| `test_models_v2.py` | 平台中立模型、能力表、状态机跃迁表、异常体系 |
| `test_core_service.py` | 去重 / 排序 / 游标恢复 / **并发** / 状态一致性 / 错误传播 / 认证自愈 / 能力缺口 |
| `test_integrations.py` | OneBot 11 与 Satori 的事件形状、API 动作、共享集成设施 |
| `test_rest_v2.py` | v2 端点、统一信封、错误码映射、**SDK 契约** |
| `test_provider_contract.py` | **同一套断言**跑在 `FakeProvider` 与真实 `SeewoProvider` 上 —— 平台契约不靠自觉 |
| `test_message_service.py` | v1 数据源层：格式化、缓存感知、批量落盘 —— **一律走公开接口**（`fetch_latest` / `load_local` / `persist`），不碰私有成员 |
| `test_layering.py` | **架构守卫**：依赖方向、平台专有模块隔离、无逆向依赖、无 TODO、文件数上限 |
| `test_protocol_endpoints.py` | 协议端点**真的挂上了**（含两个 WS 路由） |

**写测试的纪律**：

- 断言要落在**公开契约**上，不要把当前实现的偶然细节固化成预期。改实现前先确认断言的是需求而非实现。
- **不要触碰私有成员**（`_messages` / `_refresh` 这类下划线开头的东西）。用公开方法观察行为。既有 `test_message_service.py` 曾有这个毛病，已按 [docs/TEST_REVIEW.md](docs/TEST_REVIEW.md) 的审查结论整改为公开路径断言，**新增测试不要重复该模式**。
- 能力相关的用例要写**正反两面**：能力可用时行为正确 + 能力不可用时抛 `CapabilityError`（而不是静默成功或静默失败）。
- **不要用 `pytest.raises(Exception)`** —— 盲捕异常，任何异常都能通过。要断言具体异常类型，必要时连异常属性一起断言（如 `exc.value.capability`）。
- 协议适配层用 `FakeProvider` 测试，**不连真实希沃**。
- 契约测试（`test_provider_contract.py`）新增断言时，要同时对 `FakeProvider` 和 `SeewoProvider` 成立。
- 断言要有约束力：检查过 `continue` 之后的 `assert` 是否真的可达，别留下空断言。

### 日志

- `api_server.py` 启动时按 `config.json:log_level` 配置 `logging.basicConfig`，所有模块用 `logging.getLogger("seewo.xxx")`。
- `main.py` 用 `funcs.logw` 写 `logs/<date>.log`。
- Flask/Werkzeug 默认日志已降到 WARNING。
- DEBUG 级别下输出请求/响应摘要（响应截断 200~300 字）。

### 常见调试场景

- **客户端连不上**：检查 `api_server` 是否启动、`api_port` 配置、`api_key` 是否一致
- **Token 过期**：删 `tokens.json` 重新扫码；路径 B 调 `/api/login/qrcode` + `/api/login/status`。
- **mock 数据污染真实记录**：检查 `funcs.chat_log_file()` 是否随 `config.use_mock` 切换；任何新代码必须用 `funcs.chat_log_file()` 不要硬编码、也不要缓存成模块常量。
- **HTTP 429 风控**：`request_manager` 自动退避重试 3 次；若仍失败，提高 `MIN_INTERVAL` 或 `sync_all` 的 `delay` 参数。
- **顺序错乱**：检查是否漏了 `sort(key=lambda m: m["id"])`。

## Git 工作流约束

- 使用 Conventional Commits 风格（中文 body），多行描述背景、方案、兼容性考虑
- 用户要求审阅 commit message **之前**不要执行 commit
- 示例：

  ```text
  feat(api): 长消息 split 策略支持按正则智能拆分点

  为 api_server.py 的 split 策略新增按正则匹配点智能拆分的能力，
  避免在消息中间硬切。

  新增 _split_long_message 函数：在前 199 字符范围内取最后一个匹配点
  （start>0 避免空段），在匹配前切割，匹配到的字符整体跟到下一段开头
  不丢失；无匹配或唯一匹配落在段首时回退到硬切 199 字。

  新增配置项 long_message_split_pattern，使用正则表达式匹配拆分点，
  默认 \r?\n 覆盖 Windows(CRLF) 与 Unix(LF) 两种主流换行。正则编译
  失败时记 warning 并回退硬切，不影响服务启动。
  ```

## 特别注意事项

### 不得硬编码状态文件名

所有状态文件（`config.json` / `tokens.json` / `uploads.json` / `chat_history*.json` / `qrcode.png` / `logs/`）都必须通过 `init.project_path()` 或 `funcs.chat_log_file()` / 现有常量引用，**禁止在代码里直接写文件名或相对路径**。路径已由 `project_path()` 基于 `__file__` 锚定到项目根目录，硬编码会破坏该锚定、且无法在 mock 模式下自动切换。

### 配置读后即过期，禁止缓存复用

`init.config` 是 `models.Config` 模型。**从 `config.<字段>` 读出的值一离开读点即视为过期，不得缓存在模块常量/变量中复用**，否则热重载不生效。`banPaiConfig` 用 `config.ban_pai_config`。

### 199 字截断是服务器约束的客户端镜像，不可移除

> 希沃服务器对 `POST_KIDNOTE_V1_NOTE` 强制 200 字硬上限，超长返回 `statusCode=40000 / "留言内容不能超过200字符"`。`196 + "..." = 199` 是有意为之，留 1 字给省略号标记并保留 <200 的余量。**移除截断会导致 api_server 返回 40000 错误、main 路径消息静默丢失**。已于 2026-08-18 用 500 字消息实测确认。

### 共享 tokens.json 的写冲突风险

所有脚本共享 `tokens.json`：多次 QR 扫码会覆盖前一份 token。两条路径同时运行可能造成冲突，**避免同时运行多个需要登录的脚本**。

### 修改 MessageDataSource 的注意事项

- `_refresh` 通过 mtime **与文件大小**感知外部写入（main.py 的 append_message）。**写操作后必须调 `_invalidate` 失效缓存**，否则下次 `_refresh` 不会重读。
  - 已知边界：mtime 与大小都相同但内容变了（同秒内等长覆写）仍会漏判 —— 彻底解决需要文件锁或版本号。当前不宣称覆盖此情形。
- `persist(raw_messages)` 是公开方法，已封装：格式化 + merge + invalidate + refresh。测试与外部调用方都应走它，不要直接调私有的 `_format_msg`。
- 不要在 datasource 里直接调 `session.stu_msg.send`。这是数据源层，不是发送层。
- `sync_all(delay)` 的 `delay` 是同步流程自身额外增加的等待（防风控）；请求层自身的节流（`request_manager.MIN_INTERVAL`）独立叠加，不再与该常量比较。

## 已知问题

- [auto_attend.py](auto_attend.py) 粗糙非生产级：硬编码 `classlist[35]`；事件选择靠 `sys.argv[1]` + `events[1]` 回退，`events` 为空时与 owner 原版一样照常 `IndexError` 崩溃。依赖模块 `yunban_token` 能连生产取数。
- [yunban.py](yunban.py) 的所有请求**直接 `requests.request`，绕过 `request_manager`**：无统一节流 / 429 退避。批量取出全部班级、逐班取学生、批量签到等高频场景需调用方自行 `time.sleep` 防风控（503/504）。
- **聊天记录损坏 = 启动即失败（已修，故意不吞异常）**：`funcs.load_chat_history()` 遇到「文件存在但无法解析」会抛 `CorruptChatHistoryError`，让服务启动时立刻崩掉。原因：读容错（返回 `[]`）+ [append_message](funcs.py) 的「读 → 追加 → 全量覆盖」会把损坏当空记录，一次追加就把全部历史覆盖成 1 条新消息（已实测复现）。恢复方式：修复或删除损坏文件后重启，再用 `sync_all` 重新拉取全量。
- **upload policy 字段位置已按生产实测对齐（已结）**：生产实测（一次只读策略请求，不上传文件）确认 `expireSeconds` 在响应的 `data` 层，`policyList[0]` 含 `uploadUrl/headerFields/priority/type/formFields`。[mock_server.py](mock_server.py) 已按此修正，`tests/test_upload.py::test_mock_server_policy_matches_client_expectations` 已解除 skip
- **上游错误处理尚未体系化（v1 侧）**：已完成「请求超时（`request_manager.TIMEOUT`）」「状态码语义统一到 `models.SeewoCode` / `SeewoQrCode` + `SendResult` 分存 `seewo_code`/`http_status`」「`-500/-505` 刷新会话后重发一次」「媒体端点不再吞掉发送失败」「`Upload` 策略失败降级」「上传实现合并为 `upload.upload_file` 单一入口」「`login.py` 的 4 处裸 `requests.get` 加 `timeout=15`」「`msg.py` 的 `print` 改为 `logging`」。v2 侧已完整建模为异常体系（`seewo/errors.py`）。v1 侧 DAO 层仍返回结果对象而非抛异常。
- **ruff BLE001（盲捕 Exception）暂时全局忽略**：约 38 处 `except Exception`（api_server handler / TUI 事件兜底 / main 重连循环）需按调用点逐个收窄，属「上游错误处理体系化」的一部分；完成收窄后删除 [pyproject.toml](pyproject.toml) `[tool.ruff.lint]` 中的 `ignore = ["BLE001"]`
- **协议端点鉴权复用 `api_key`**：OneBot 11 / Satori 端点复用 `config.api_key` 鉴权（每次请求实时读取，热重载即时生效）：OneBot 用 `Authorization: Bearer` 或 `?access_token=`，Satori HTTP 用 `Authorization: Bearer`，Satori WS 在 IDENTIFY 通过前不推送事件。 `api_key` 为空串时所有端点放行（与 v1 不同），部署时务必设置。
- **v2 没有后台轮询**：入站消息只在调用 `/api/v2/sync` 或 `GET /api/v2/messages` 时产生；需要持续接收由外部调度器定时调用。
- **v2 / 协议端点无法发送媒体**：`api_server` 未给 v2 Provider 接上传器（接上会让已鉴权客户端指定服务端任意本地文件上传到希沃云），`send_image/send_audio` 如实申报 `false`。媒体请走 v1 `/api/send_image` / `/api/send_audio`。
- **WebSocket 端点依赖 Werkzeug 开发服务器**：`flask-sock` 在 `app.run()` 下正常；生产部署需换成支持 WS 的 WSGI 服务器（如 gunicorn + gevent），否则 WS 握手失败（HTTP 端点不受影响）。
- **希沃协议能力缺口（非待办，是协议限制）**：撤回 / 提及 / 引用 / 视频与文件发送 / 流式推送均未实现。协议层不具备或**未经验证**，不猜测行为。完整清单见 [docs/CAPABILITIES.md](docs/CAPABILITIES.md)。

## 数据文件清单

| 文件 | 说明 | 路径 |
| --- | --- | --- |
| `config.json` | 配置文件（从 `.example` 复制） | 两条路径 |
| `tokens.json` | 登录 Token 存储 | 两条路径（共享，注意写冲突） |
| `chat_history.json` | 真实聊天记录持久化 | 两条路径 |
| `chat_history_mock.json` | Mock 模式下的聊天记录 | Mock（`use_mock=true` 时自动切换） |
| `chat_history.v2.json` | v2 服务独立的消息存储（**不读写** v1 的 `chat_history.json`） | 路径 B 的 v2 / 协议端点 |
| `chat_history*.v2.json.cursor` | v2 游标（断线恢复） | 同上 |
| `uploads.json` | 上传文件记录（dict，basename 冲突时使用 `#2`、`#3` 后缀） | 路径 A（`upload_file.py`）/ 路径 B（`/api/send_image` 等） |
| `mock_data.json` | Mock 服务器持久化数据 | Mock |
| `logs/*.log` | 按日期记录日志 | 两条路径 |
| `qrcode.png` | 登录二维码图片 | 两条路径（登录时生成） |

## 配置文件字段速查（`config.json`）

```json
{
  "api_key": "your-secret-key",            // 路径 B 客户端鉴权
  "api_port": 5001,                        // 路径 B 服务端口（client/tui/test_api 默认回退已对齐 5001；test_api.py 现位于 test/）
  "api_host": "0.0.0.0",                   // 路径 B 服务主机
  "poll_batch_size": 50,                   // 路径 A 轮询批量大小
  "base_interval": 1,                      // 路径 A 基础轮询间隔(秒)
  "max_interval": 10,                      // 路径 A 最大轮询间隔(秒)
  "max_errors": 5,                         // 路径 A 连续错误上限（触发重连）
  "use_mock": false,                       // 启用 Mock 服务器（跨两条路径）
  "mock_port": 9000,                       // Mock 服务器端口
  "long_message_strategy": "truncate",     // 仅路径 B 的 /api/send；main.py 不读
  "long_message_split_pattern": "\\r?\\n", // 仅路径 B split 策略；JSON 反斜杠需双写
  "log_level": "INFO"                      // 日志级别（跨两条路径）
}
```

## 进一步参考

- [README.md](README.md) — 用户向使用文档
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — `seewo/` 分层、依赖方向、数据流、不变量清单
- [docs/INTEGRATION.md](docs/INTEGRATION.md) — OneBot 11 / Satori 接入实操、自写适配器指南
- [docs/MIGRATION.md](docs/MIGRATION.md) — v1 → v2 迁移指南、兼容性保证
- [docs/CAPABILITIES.md](docs/CAPABILITIES.md) — 能力声明与已知能力缺口
- [test/test_api.py](test/test_api.py) — API 端点逐个测试脚本
