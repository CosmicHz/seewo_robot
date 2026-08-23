# AGENT 开发指南

> 这是给 AI 助手的完整项目上下文。读完后应能直接接手开发，不需要再读源码就能动手改任意模块。

## 项目定位

希沃云班「亲情留言」聊天机器人。通过「希沃统一服务平台」API，以家长身份收发留言、执行命令、上传媒体、查询考勤。所有外部请求的目标服务器是希沃生产服务（`m-campus.seewo.com` / `campus.seewo.com` / `id.seewo.com`），可在 `config.json` 切到本地 mock 服务器调试。

## 快速开始（拿到代码后）

```bash
# 1. 安装依赖（uv 优先）
uv sync          # 或 pip install -r requirements.txt

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
uv run python test_api.py        # 逐个测试所有 API 端点
uv run python probe_kidnote_api.py  # 批量探测 /api/messages（含 SQL payload）
```

**Windows 注意**：PowerShell 无 `cat`，commit message 用多个 `-m` 或临时文件；`&&`/`||` 不是分隔符，用 `;`。

## 两条独立运行路径（最高优先级约束）

项目存在**两条代码路径互相独立**的运行路径：**内存中的会话对象各自维护**（路径 A 全局 `account/student/stu_msg`；路径 B `Session` 对象），但**共享磁盘上的状态文件**（`tokens.json` / `chat_history.json` / `config.json` / `uploads.json` / `logs/`）。任一路径扫码后写回的 `tokens.json` 可被另一路径直接读取复用，**只有凭证不存在或失效时才需扫码**。

### 路径 A：独立脚本（直连希沃）

| 脚本 | 职责 |
| --- | --- |
| [main.py](main.py) | 常驻轮询监听最新留言 + 处理 `/` 开头命令 + 自适应轮询间隔 + 断线重连 |
| [send_msg.py](send_msg.py) | 一次性登录并发送一条文本 |
| [upload_file.py](upload_file.py) | 一次性登录并上传文件至希沃云存储接口 |
| [auto_attend.py](auto_attend.py) | 自动签到（**当前损坏**，见已知问题） |

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
基础设施 (init.py / login.py / funcs.py)  ← 配置、登录、文件读写工具
```

### 各层职责（关键）

- **[request_manager.py](request_manager.py)**：所有对希沃的 HTTP 请求**必须**经此调度。当前实现 `_throttle()` 全局节流（`MIN_INTERVAL=0.5s`，`threading.Lock` 串行化节流点）+ HTTP 429 指数退避重试（3 次）。预留队列化接口（`queue.Queue + worker`，当前同步执行）。**新增任何对希沃的 `requests.post` 都必须改用 `request_manager.post`**。
- **[api.py](api.py)**：m-campus API 调用网关。`api().action(type, params, account)` 把 pxencode 后的参数 POST 到 `/class/apis.json?action=<type>`，返回响应 JSON。内部已用 `request_manager.post`。
- **[msg.py](msg.py)**：纯 DAO。只做单次请求，**不再有**多页聚合逻辑。`get(count, start=1)` 是核心方法，`start` 是 1-based 页码（start=1=最新一页，递增往更旧翻页，页内按 id 升序，页间无重叠）。`get_last` / `get_content` 标记 `极不完善，请勿使用`。
- **[message_service.py](message_service.py)**：消息数据源层。`MessageDataSource` 独占 `chat_history.json` 读写、消息格式化、内存缓存（基于文件 mtime 感知 `main.py` 等外部写入）。`sync_all` 翻页循环在本层。
- **[api_server.py](api_server.py)**：入口层，handler 薄层化。全局 `session = Session()` + 全局 `datasource = MessageDataSource(session)`（构造时仅 `_refresh` 读 mtime，不碰 session）。
- **[init.py](init.py)**：全局配置 `config`、文件路径、`_use_mock`、公共请求头 `headers_nocookie`、希沃 URL 集合 `urls`。
- **[login.py](login.py)**：`acc` 账户对象 + `download_qrcode` / `check_qrcode` / `login` 流程。`acc(auto_login=True/False)` 控制过期时是否自动触发扫码：`main.py` 用 True，`api_server.py` 用 False。
- **[stu.py](stu.py)**：学生信息 DAO，默认 `count=0` 取列表第一个学生（[main.py:9](main.py#L9) 留有 `TODO: 多学生选择`）。
- **[funcs.py](funcs.py)**：工具函数。`CHAT_LOG_FILE` 根据 `_use_mock` 切换 `chat_history_mock.json` / `chat_history.json`。`load_chat_history` / `append_message` / `merge_messages` 只维护 `messages` 字段，不再维护 `earliest_id` / `last_id`。
- **[upload.py](upload.py)**：文件上传到希沃云存储接口，返回 `downloadUrl`。
- **[yunban.py](yunban.py)**：云班功能扩展（班级列表、考勤事件、签到等）。`getpass` 用于获取离线验证码，`getpass2` 依赖 pandas 剪贴板（**损坏**）。
- **[qrcode.py](qrcode.py)**：终端二维码渲染（依赖 `numpy` + `pillow`）。

## 核心 API 端点（路径 B）

所有 `/api/*` 接口需 `X-API-Key` header 认证（`config.json:api_key`）。端口默认 5001（`config.json:api_port`）。

| 端点 | 方法 | 数据源层方法 | 说明 |
| --- | --- | --- | --- |
| `/api/status` | GET | — | 服务状态 + 学生信息 |
| `/api/messages` | GET | `datasource.fetch_latest(count)` | 实时向希沃取最新一页，**不持久化** |
| `/api/send` | POST | `session.stu_msg.send` | 文本发送，支持长消息 `strategy` |
| `/api/send_image` | POST | `upload_file_to_cloud` + `stu_msg.send` | 图片发送 |
| `/api/send_audio` | POST | `upload_file_to_cloud` + `stu_msg.send` | 音频发送 |
| `/api/history` | GET | `datasource.load_local(offset, limit)` | 读本地缓存，分页 |
| `/api/load_earlier` | GET | `datasource.load_earlier_from_local(before_id, count)` | 纯本地读更早消息（不请求希沃，靠 `sync_all` 提前同步） |
| `/api/sync_all` | POST | `datasource.sync_all(batch_size, delay)` | 全量同步历史到本地（防风控） |
| `/api/refresh` | POST | `session.refresh()` | 重新初始化会话 |
| `/api/login/qrcode` | GET | `download_qrcode` + 后台 `_poll_login` 线程 | 获取登录二维码（Base64），同时启动后台轮询（不阻塞服务） |
| `/api/login/status` | GET | — | 查询扫码状态（`idle` / `pending` / `ok` / `error`） |
| `/api/execute` | POST | `os.popen` | 执行命令（受 `allowed_prefixes` 白名单限制：`getpass`、`发送音乐`） |

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

### earliest_id / last_id（**重要**）

- **不再作为文件字段维护**。`load_chat_history` 只返回 `{"messages": [...]}`，不推断 earliest_id / last_id。
- 调用方（`main.py` / `api_server.py` / `tui_client.py`）**需要时自行从 messages 推断**：

  ```python
  last_id = max(m["id"] for m in messages) if messages else 0      # 最新
  earliest_id = min(m["id"] for m in messages) if messages else 0  # 最旧
  ```

- 服务器响应体**不包含** earliest_id / last_id 字段，客户端自行从 messages 推断。

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

### 希沃 statusCode 语义

| code | 含义 | 处理 |
| --- | --- | --- |
| 200 | 成功 | 正常返回 |
| -500 | Token 无效 | 重新登录 |
| -505 | Token 过期 | 重新登录 |
| 40000 | 业务校验失败（如留言超 200 字） | 客户端层修复 |
| 50000 | 服务器内部错误（如 SQL 错误） | 检查请求参数（如 `start<1` 触发） |

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
- **聊天记录隔离**：mock 模式下 `funcs.py` 自动把 `CHAT_LOG_FILE` 切到 `chat_history_mock.json`，避免测试数据污染真实 `chat_history.json`。
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
    err = _check_session()      # 1. 会话检查（必须）
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

- [test_api.py](test_api.py)：逐个测试所有 API 端点，发送/图片/音频会跳过避免误发。
- [probe_kidnote_api.py](probe_kidnote_api.py)：批量探测 `/api/messages`，支持多种 payload 类型（含 SQL 注入测试）。

### 日志

- `api_server.py` 启动时按 `config.json:log_level` 配置 `logging.basicConfig`，所有模块用 `logging.getLogger("seewo.xxx")`。
- `main.py` 用 `funcs.logw` 写 `logs/<date>.log`。
- Flask/Werkzeug 默认日志已降到 WARNING。
- DEBUG 级别下输出请求/响应摘要（响应截断 200~300 字）。

### 常见调试场景

- **客户端连不上**：检查 `api_server` 是否启动、`api_port` 配置、`api_key` 是否一致
- **Token 过期**：删 `tokens.json` 重新扫码；路径 B 调 `/api/login/qrcode` + `/api/login/status`。
- **mock 数据污染真实记录**：检查 `funcs.py:CHAT_LOG_FILE` 是否正确切换；任何新代码必须用 `funcs.CHAT_LOG_FILE` 不要硬编码。
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

## 特别注意事项（必读）

### 硬约束：永远不要改变 main.py 的行为

> 任何改动、重构、bug 修复都不得影响 `main.py` 的运行逻辑与对外行为。
> 涉及共享底层模块（`login` / `stu` / `msg` / `funcs` / `init`）的修改，必须保证 `main.py` 调用路径上的语义不变；只能新增或修改 `api_server` 路径专用的逻辑。

具体含义：

- `main.py` 的长消息处理走**硬编码 `[:196] + "..."`**，不读 `long_message_strategy` / `long_message_split_pattern`，**不可改变**
- `main.py` 的 `earliest_id` / `last_id` 从 `messages` 推断（[main.py:113-114](main.py#L113-114)），**不可退回文件字段**
- `main.py` 调 `msg.get()` 的语义不可变（`start` 是 1-based 页码）
- 修改共享模块时，先确认 `main.py` 的调用路径不受影响

### 199 字截断是服务器约束的客户端镜像，不可移除

> 希沃服务器对 `POST_KIDNOTE_V1_NOTE` 强制 200 字硬上限，超长返回 `statusCode=40000 / "留言内容不能超过200字符"`。`196 + "..." = 199` 是有意为之，留 1 字给省略号标记并保留 <200 的余量。**移除截断会导致 api_server 返回 40000 错误、main 路径消息静默丢失**。已于 2026-08-18 用 500 字消息实测确认。

### 生产环境 SQL 注入风险评估（不重要）

经实测确认生产环境有三层防护：

1. **WAF**（阿里云）：布尔型 SQL payload（如 `1 OR 1=1`）会被 WAF 拦截返回 HTTP 405
2. **应用层参数校验**：非数字 `start` 被 `int()` 转换失败拦截（`请求参数校验失败`）

唯一问题：`start=0` 缺少边界检查会触发 SQL 错误（负 offset）。批量测试脚本留存在 [probe_kidnote_api.py](probe_kidnote_api.py)。

### 共享 tokens.json 的写冲突风险

所有脚本共享 `tokens.json`：多次 QR 扫码会覆盖前一份 token。两条路径同时运行可能造成冲突，**避免同时运行多个需要登录的脚本**。

### chat_history.json 的 mock 数据污染（已修复，但需警惕）

历史问题：mock 模式下测试数据会污染真实 `chat_history.json`。**当前已通过** `funcs.py` 的 `CHAT_LOG_FILE` 切换实现隔离（mock → `chat_history_mock.json`）。任何新增的聊天记录读写代码**必须**用 `funcs` 暴露的 `CHAT_LOG_FILE`，不要硬编码文件名。

### 修改 MessageDataSource 的注意事项

- `_refresh` 通过 mtime 感知外部写入（main.py 的 append_message）。**写操作后必须调 `_invalidate` 失效缓存**，否则下次 `_refresh` 不会重读。
- `_persist` 已封装：格式化 + merge + invalidate + refresh。
- 不要在 datasource 里直接调 `session.stu_msg.send`——这是数据源层，不是发送层。

## 已知问题

- [auto_attend.py](auto_attend.py) 损坏：第 4 行 `from yunabn_token import test` 模块不存在（疑似应为 `yunban`），脚本无法运行
- [yunban.py](yunban.py) 的 `getpass2` 依赖 `pandas.io.clipboard`，但 `pyproject.toml` / `requirements.txt` 未含 pandas
- [message_service.py:157-159](message_service.py#L157-L159) 的 `if delay > request_manager.MIN_INTERVAL` 是耦合传输层细节的实现，理想做法是直接 `time.sleep(delay)` 让 request_manager 节流叠加，或完全移除让 request_manager 单独节流
- **uploads.json 按纯文件名做 key，同名覆盖**：写入逻辑在 [upload.py#L121-L122](upload.py#L121-L122)，key 只取 `os.path.basename(file)`，不区分完整路径、不区分家长账号。同一个 basename 的文件重复上传（哪怕路径不同、账号不同、内容不同），后写的都会直接覆盖前一条。影响范围有限：主业务链路（上传 → 发送留言）走内存中的 `downloadUrl`，不读这份台账，因此不会导致发错、漏发；只会影响"事后手动打开 uploads.json 按文件名翻历史上传 URL"的查询场景。暂时不打算改文件格式（避免破坏已按旧 dict 格式写了解析逻辑的其他开发者脚本），如要修复需提前发布变更公告。

## 数据文件清单

| 文件 | 说明 | 路径 |
| --- | --- | --- |
| `config.json` | 配置文件（从 `.example` 复制） | 两条路径 |
| `tokens.json` | 登录 Token 存储 | 两条路径（共享，注意写冲突） |
| `chat_history.json` | 真实聊天记录持久化 | 两条路径 |
| `chat_history_mock.json` | Mock 模式下的聊天记录 | Mock（`use_mock=true` 时自动切换） |
| `uploads.json` | 上传文件记录（dict，key 仅取文件 basename，同名上传会覆盖，详见已知问题） | 路径 A（`upload_file.py`）/ 路径 B（`/api/send_image` 等） |
| `mock_data.json` | Mock 服务器持久化数据 | Mock |
| `logs/*.log` | 按日期记录日志 | 两条路径 |
| `qrcode.png` | 登录二维码图片 | 两条路径（登录时生成） |

## 配置文件字段速查（`config.json`）

```json
{
  "api_key": "your-secret-key",            // 路径 B 客户端鉴权
  "api_port": 5001,                        // 路径 B 服务端口（client/tui/test_api 默认回退已对齐 5001）
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
- [test_api.py](test_api.py) — API 端点逐个测试脚本
