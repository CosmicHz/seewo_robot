# Seewo 班牌机器人

> [!WARNING]
> **该项目正在开发中，请勿用于学习环境，否则可能会带来严重后果！**

## 这是什么？

Seewo 班牌机器人是一个对接**希沃云班**小程序的聊天工具。它以**家长身份**登录希沃统一服务平台，实现与孩子班牌之间的「亲情留言」互动——收消息、发消息（文本 / 图片 / 音频）、甚至通过留言下发指令。

项目内置了**两种使用方式**（两条独立的运行路径），你可以根据场景选择：

| 使用方式 | 适合场景 | 特点 |
| --- | --- | --- |
| **独立脚本**（路径 A） | 快速发一条消息、挂在后台长期监听 | 每个脚本直接登录希沃，无需额外服务 |
| **客户端-服务端**（路径 B） | 日常使用、二次开发、集成到其他系统 | 只需服务端登录一次，客户端经 HTTP 调用；支持图片/音频发送、全量同步、TUI 界面等 |

两条路径**共享同一个配置文件和数据文件**（如 `tokens.json`、`chat_history.json`），但内存中的登录会话各自独立。

## 功能一览

- **微信二维码扫码登录**（无需账号密码）
- **消息实时监听**（自适应轮询间隔 + 断线自动重连）
- **发送留言**：
  - 文本（支持超长消息的 **截断** 或 **智能拆分多条** 两种策略）
  - 图片
  - 音频
- **命令执行**：收到以 `/` 开头的留言时执行指定命令（如获取离线验证码、批量发送音乐等）
- **聊天记录持久化**：所有消息保存到本地 JSON，可随时查看历史
- **全量同步历史消息**：把希沃服务器上的历史消息一次性拉到本地（带防风控延迟）
- **TUI 终端图形界面**：基于 Textual，支持快捷键操作
- **CLI 命令行 + Python SDK**：方便脚本调用或二次开发
- **Mock 本地调试服务器**：不连接真实希沃服务器也能完整测试所有功能

## 环境要求

- **Python 3.12+**（项目使用了 match-case 等新语法）
- 推荐依赖管理工具：**[uv](https://docs.astral.sh/uv/)**（也兼容 pip）

## 快速开始

### 1. 安装依赖

```bash
# 推荐：使用 uv（自动按 uv.lock 锁定版本，最稳定）
uv sync

# 或使用 pip
pip install -r requirements.txt
```

> [!IMPORTANT]
> **依赖管理约定**
>
> - 项目依赖的**唯一来源**是 [`pyproject.toml`](pyproject.toml)，请在那里声明依赖。
> - [`requirements.txt`](requirements.txt) 是**锁文件**（生成产物），**请勿手动修改**；它是给 `pip install -r requirements.txt` 用的兼容快照。
> - 开发依赖（`pip-tools` / `ruff` / `pytest`）已 pin 版本声明在 `pyproject.toml` 的 `dev` 组。
> - 修改 `pyproject.toml` / `uv.lock` 后，`pre-commit` 钩子在提交时自动用 `pip-compile` 重新生成并暂存 `requirements.txt`；**若本机未安装 `pip-tools`，提交会被阻止**（勿用 `--no-verify` 绕过，CI 会拦截不同步的锁文件）。
> - 如需手动重新生成（确保 `pip-tools` 已安装；`PIP_CONFIG_FILE=/dev/null` 用于隔离本机镜像配置，避免个人 pip 源写进锁文件）：
>
>   ```bash
>   PIP_CONFIG_FILE=/dev/null pip-compile --no-header --no-emit-index-url --strip-extras --output-file requirements.txt pyproject.toml
>   ```

核心依赖说明：

- `requests`、`pillow`、`requests-toolbelt` —— 基础功能必需
- `flask` —— 路径 B 的 REST API 服务端
- `textual` —— 路径 B 的 TUI 客户端
- `numpy` —— 终端二维码渲染

### 2. 准备配置文件

项目根目录有一份示例配置 `config.json.example`，请复制为 `config.json` 后按需修改：

```bash
# Linux / macOS
cp config.json.example config.json

# Windows PowerShell
Copy-Item config.json.example config.json
```

最关键的两项：

- `api_key`：给路径 B 的客户端-服务端鉴权用，自己写一个字符串就行
- `api_port`：服务端监听端口，默认 **5001**（注意：不是 5000）

完整配置项见 [配置详解](#配置详解configjson)。

### 3. 选择一种方式开始使用

> **注意**：各脚本共享同一个 `tokens.json`，不要同时运行多个脚本以免互相覆盖登录态。

## 使用方式 A：独立脚本（直连希沃）

每个脚本运行时各自完成扫码登录，直接请求希沃服务器。适合临时用一下，或者挂后台长期监听。

### 📻 `main.py` — 常驻消息监听 + 命令处理

最经典的用法：终端里挂着，自动轮询有没有新留言，有就打印出来；收到以 `/` 开头的留言还会执行命令。

```bash
# 使用 uv 运行
uv run main.py

# 或直接使用 Python
python main.py
```

> [!NOTE]
> 下文中所有涉及执行 Python 程序的终端命令都默认使用 `uv run <程序名>` 演示，可自行替换为 `python <程序名>`。

**首次运行**：终端会弹出二维码（或生成 `qrcode.png` 图片），用**微信**扫码登录（就像你登录希沃云班小程序一样）。登录成功后凭证写入 `tokens.json`，下次自动读取不用再扫。

**行为特点**：

- **自适应轮询**：没新消息时，轮询间隔从 1 秒逐步放宽到 10 秒，降低服务器压力；一来新消息立刻恢复 1 秒间隔。
- **断线自动重连**：连续出错 5 次后自动重新走扫码流程。
- **首次初始化**：如果本地没有聊天记录，自动拉取最新 100 条存底。
- **长文本截断**：发送文本超过 199 字时，自动截断为前 196 字 + `...`（这是硬编码行为，不读配置）。

### 💬 `send_msg.py` — 一次性快速发送文本

发完就退出，不需要挂进程。

```bash
uv run send_msg.py "今天放学早点回来，路上注意安全"
```

### 📎 `upload_file.py` — 一次性上传媒体文件（不自动发送）

```bash
uv run upload_file.py ./照片.jpg
```

关于这个脚本需要说明的几件事：

- **上传完成后不会自动作为留言发送**：它只是把文件通过希沃的**云存储接口**推上去，然后在终端打印一个公网可访问的 `downloadUrl`。要把它作为图片/音频/视频留言发给孩子，还需要你自己用这个 `downloadUrl` 构造一次 `msg.py` 里的 `send(type=2/3/4, resUrl=...)` 调用，或者直接走路径 B 的 `/api/send_image` / `/api/send_audio`（它们内部一步完成上传 + 发送）。
- 上传成功的完整返回（含 `downloadUrl`、`fileId` 等）会按文件名作为 key 写入 `uploads.json`，方便以后查 URL，不会被聊天链路自动引用。

### ⚙️ `auto_attend.py` — 批量考勤签到工具（次要功能，缺乏维护，当前损坏）

这是一个独立的批量考勤签到工具，不使用项目统一的扫码登录机制，也不直接服务于聊天主流程。详见 [维护状态](#维护状态) 说明。

---

## 使用方式 B：客户端-服务端（HTTP 中转）⭐ 推荐

把登录和希沃 API 调用都交给 `api_server.py` 网关统一处理，客户端只跟本地 HTTP 接口通信。好处是：

- 服务端登录一次，多个客户端共享
- 客户端不接触希沃服务器、不需要扫码凭证
- 支持更丰富的功能：图片发送、全量同步、按需加载历史、TUI 界面、长消息智能拆分等

### 🖥️ 第 1 步：启动 API 服务端

```bash
uv run api_server.py
```

启动后会打印：

```text
==================================================
希沃班牌机器人 API 服务
==================================================
API Key: your-secret-key
端口: 5001
主机: 0.0.0.0
[MOCK] 未启用，连接真实服务器
==================================================
```

> **登录机制**：与 main.py 不同，服务端默认 `auto_login=False`——如果 `tokens.json` 不存在或过期，不会自动弹二维码阻塞服务，而是返回 `need_login=true`，由客户端（TUI 或 CLI）引导你调用 `/api/login/qrcode` + `/api/login/status` 完成异步扫码登录。

### 📱 第 2 步：选择一个客户端使用

#### 选项 1：TUI 终端图形界面（最直观）

```bash
uv run tui_client.py
```

界面从上到下分为三部分：

- **消息列表**（可滚动）：家长消息靠右、学生消息靠左，不同颜色区分
- **输入区**：输入框 + 「拆分模式」复选框 + 发送按钮
- **状态栏**：显示连接状态

**TUI 快捷键一览**：

| 按键 | 功能 |
| --- | --- |
| `R` | 刷新消息（从服务端拉最新） |
| `H` | 查看本地历史聊天记录 |
| `L` | 加载更早的消息（游标翻页） |
| `Y` | 全量同步所有历史消息到本地 |
| `S` | 发送当前输入框里的文本消息 |
| `I` | 发送图片（选文件） |
| `A` | 发送音频（选文件） |
| `Q` | 退出 |

「**拆分模式**」复选框：勾选后本次发送长消息会强制使用 `split` 策略（多条拆分），不勾选则由服务端全局配置（默认 `truncate` 截断）决定。

#### 选项 2：CLI 命令行（脚本化 / 批处理）

```bash
# 查看服务状态（是否已登录、学生信息等）
uv run client.py status

# 拉取最新 20 条消息
uv run client.py messages --count 20

# 发送文本（超长走服务端全局策略）
uv run client.py send "你好，最近学习怎么样？"

# 发送长文本，显式指定 split 拆分多条
uv run client.py send "非常长的一段话..." --strategy split

# 发送图片
uv run client.py image ./照片.png

# 发送音频（--length 是毫秒数，默认 666）
uv run client.py audio ./语音.mp3 --length 3000

# 查看本地聊天记录
uv run client.py history --limit 100 --offset 0

# 刷新会话（token 过期时用）
uv run client.py refresh
```

#### 选项 3：作为 Python SDK 在代码中调用

把 `client.py` 当作库 import 就行：

```python
from client import SeewoClient

c = SeewoClient()  # 连接网关
# 也可以自定义：SeewoClient(base_url="http://localhost:5001", api_key="xxx")

# 查状态
print(c.get_status())

# 发文本（长消息默认截断）
c.send_text("你好")

# 发文本，显式指定拆分多条
c.send_text("非常长的一段话...", strategy="split")

# 发图片（两种方式二选一）
c.send_image(file_path="./照片.png")
# c.send_image(file_data=b'...', filename="photo.png")

# 发音频
c.send_audio("./语音.mp3", voice_length=3000)

# 看本地历史
c.get_history(limit=50, offset=0)
```

---

## 🧪 Mock 调试模式（不连真实希沃服务器）

本地开发调试时，不想每次都连真实希沃服务器、担心误发消息？用 Mock 模式。

### 启用步骤

1. **改配置**：打开 `config.json`，设置 `"use_mock": true`，并指定 `"mock_port": <端口号>`（端口自定义）
2. **启动 Mock 服务器**：

    ```bash
    uv run mock_server.py
    ```

3. **正常运行你的程序**：路径 A 的脚本、路径 B 的 `api_server.py` 都会自动把请求指向本地 Mock 服务器，不需要改其他代码。

### Mock 服务器的优势

- **行为与真实服务器一致**：包括单条留言 200 字硬上限（超长返回 `statusCode=40000`）、`start<1` 触发 SQL 错误等，能在本地复现生产环境的边缘情况。
- **聊天记录隔离**：Mock 模式下自动改用 `chat_history_mock.json` 存储，测试数据不会污染你真实的 `chat_history.json`。
- **UID 自动接管，不用手动改 tokens.json**：`use_mock=true` 后，直接带着你真实的 `tokens.json` 运行程序即可。Mock 服务器第一次收到你的请求时，会把内部预置的家长身份和消息**自动映射**到你真实的家长 UID 上——启动时不用改任何配置，就能直接看到 mock 里预设的"测试消息"。

### Mock 管理接口（注入测试数据）

Mock 服务器自带一组管理接口，方便模拟学生发消息、清空数据等。

| 接口 | 方法 | 功能 |
| --- | --- | --- |
| `/mock/add_message` | POST | 模拟学生发送一条消息 |
| `/mock/clear_messages` | POST | 清空所有消息 |
| `/mock/data` | GET | 查看当前所有 mock 数据 |
| `/mock/reset` | POST | 重置为默认状态（带预设消息） |
| `/mock/save` | POST | 持久化到 `mock_data.json` |
| `/mock/load` | POST | 从 `mock_data.json` 加载 |

**模拟学生发消息示例**（PowerShell）：

```powershell
Invoke-WebRequest -Uri "http://localhost:9000/mock/add_message" `
  -Method POST -ContentType "application/json" `
  -Body '{"content": "我在学校很好，不用担心！"}'
```

---

## 配置详解（`config.json`）

| 配置项 | 默认值 | 作用路径 | 说明 |
| --- | --- | --- | --- |
| `api_key` | `"your-secret-key"` | 路径 B | 客户端调用 `/api/*` 接口时，需要在请求头带 `X-API-Key` 与它匹配 |
| `api_port` | `5001` | 路径 B | API 服务端监听端口 |
| `api_host` | `"0.0.0.0"` | 路径 B | API 服务端监听地址 |
| `poll_batch_size` | `50` | 两条路径 | 每次轮询向希沃拉取的消息条数 |
| `base_interval` | `1` | 路径 A 轮询 / 路径 B 轮询 | 基础轮询间隔（秒） |
| `max_interval` | `10` | 路径 A 轮询 / 路径 B 轮询 | 最大轮询间隔（秒，无新消息时逐步放宽到此值） |
| `max_errors` | `5` | 路径 A 轮询 / 路径 B 轮询 | 连续出错超过此次数后触发断线重连 |
| `use_mock` | `false` | 两条路径 | 是否启用本地 Mock 服务器 |
| `mock_port` | `9000` | 两条路径 | Mock 服务器端口 |
| `long_message_strategy` | `"truncate"` | 仅路径 B `/api/send` | 长消息处理策略：`truncate`=截断为199字（默认） / `split`=拆分为多条 |
| `long_message_split_pattern` | `"\\r?\\n"` | 仅路径 B split 策略 | split 模式下的智能拆分点正则（Python 风格）。默认兼容 CRLF/LF 换行。JSON 中反斜杠需双写 |
| `log_level` | `"INFO"` | 两条路径 | 日志级别，可选 `DEBUG` / `INFO` / `WARNING` / `ERROR` |

### 关于长消息拆分正则的几个例子

- 默认 `"\\r?\\n"`：优先在换行符之前切，避免句子被拦腰截断
- `"[。！？\\n]"`：优先在中文句号、感叹号、问号或换行之前切
- `""`（空字符串）：禁用智能拆分，全程硬切 199 字

---

## REST API 接口（路径 B 独有）

所有接口需带 `X-API-Key` 请求头（或 query 参数 `?api_key=xxx`）。基础 URL：`http://localhost:5001`。

### 登录相关

| 接口 | 方法 | 说明 |
| --- | --- | --- |
| `/api/login/qrcode` | GET | 获取登录二维码（Base64 图片），同时启动后台扫码轮询线程 |
| `/api/login/status` | GET | 查询扫码状态：`idle`（无流程）/ `pending`（等待扫码）/ `ok`（成功）/ `error`（失败或超时） |

### 业务接口

| 接口 | 方法 | 说明 |
| --- | --- | --- |
| `/api/status` | GET | 获取服务状态（登录态、学生信息等） |
| `/api/messages?count=10` | GET | **实时**从希沃拉取最新一页消息（不写入本地缓存） |
| `/api/history?limit=50&offset=0` | GET | 从**本地缓存**读取聊天记录（分页） |
| `/api/load_earlier?count=50&before_id=xxx` | GET | 从**本地缓存**读取早于 `before_id` 的消息（用于滚动加载） |
| `/api/sync_all` | POST | 全量同步所有历史消息到本地缓存（body 可带 `batch_size` 和 `delay` 防风控） |
| `/api/send` | POST | 发送文本消息（详见下方「长消息处理」） |
| `/api/send_image` | POST | 发送图片（支持 JSON body 传 `file_path`，或 multipart/form-data 上传文件） |
| `/api/send_audio` | POST | 发送音频（JSON body 传 `file_path` + `voice_length`） |
| `/api/refresh` | POST | 刷新会话（重新读取 tokens.json 或重新登录） |
| `/api/execute` | POST | 执行命令（白名单限制，仅允许 `getpass`、`发送音乐` 前缀） |

### 长消息处理（`/api/send`）

希沃服务器对**单条留言强制 200 字硬上限**（超长返回 `statusCode=40000` / "留言内容不能超过200字符"）。所以客户端发送超过 199 字的文本时，服务端会按以下两种策略处理（可由全局配置或单次请求覆盖）：

#### 策略 1：`truncate`（默认）—— 截断省略

取前 196 字 + `...`，合并 199 字（留 1 字余量），单条发送。

响应示例：

```json
{
  "status": "ok",
  "strategy": "truncate",
  "truncated": true,
  "message": "发送成功"
}
```

#### 策略 2：`split` —— 智能拆分多条

按配置的 `long_message_split_pattern` 正则，在前 199 字符范围内找**最后一个匹配点**，从匹配点之前切开；匹配到的字符（如换行符）整体跟到下一段开头，不会丢失。无处可拆时回退到硬切 199 字。

响应示例（500 字 → 3 条）：

```json
{
  "status": "ok",
  "strategy": "split",
  "chunks": 3,
  "results": [true, true, true],
  "message": "已拆分为 3 条发送"
}
```

**如何指定策略**：

- 全局默认：`config.json` 的 `long_message_strategy`
- 单次覆盖：`/api/send` 请求体加上 `"strategy": "truncate"` 或 `"strategy": "split"`

---

## 命令执行（以 `/` 开头的留言）

当 `main.py`（路径 A）或 `/api/execute`（路径 B）收到**以 `/` 开头**的留言时，会尝试执行命令。受白名单限制，目前允许以下前缀：

| 命令 | 用法 | 功能 |
| --- | --- | --- |
| `/getpass` | `/getpass <schoolUid> <snCode>` | 获取离线验证码，结果直接作为回复发回 |
| `/发送音乐` | `/发送音乐` | 把项目下 `music/` 目录里的所有音频文件逐条发出（目录不存在会自动创建） |
| 其他 | 任意 shell 命令 | `main.py` 中会直接 `os.popen()` 执行并把 stdout 发回；**路径 B 的 `/api/execute` 出于安全考虑已禁用**，白名单外返回 403 |

---

## 数据文件清单

程序运行过程中会在项目根目录生成以下数据文件：

| 文件 | 说明 | 使用路径 |
| --- | --- | --- |
| `config.json` | 配置文件（从 `.example` 复制） | 两条路径 |
| `tokens.json` | 登录凭证（扫码后生成，包含家长 UID、token 等） | 两条路径（共享，注意写冲突） |
| `chat_history.json` | **真实**聊天记录持久化 | 两条路径（`use_mock=false` 时） |
| `chat_history_mock.json` | **Mock 模式**聊天记录 | Mock（`use_mock=true` 时自动切换，隔离真实数据） |
| `uploads.json` | 上传到希沃云盘的文件记录 | 两条路径 |
| `mock_data.json` | Mock 服务器持久化数据（调用 `/mock/save` 时生成） | Mock |
| `qrcode.png` | 登录时生成的二维码图片 | 两条路径 |
| `logs/*.log` | 按日期切分的日志文件 | 两条路径 |

> [!important]
> 两条路径虽然会话独立，但**共享磁盘上的所有数据文件**（`tokens.json`、`chat_history.json`、`uploads.json` 等）。不同时跑多个需要登录的进程，避免 token 被互相覆盖。
>
> 此外项目是**单家长账号 + 单学生**假设设计的：没有多账号、多学生的命名空间隔离或切换机制。切账号 / 切学生会带来数据混杂、消息漏拉等风险，详见 [设计限制](#设计限制当前没有的功能)。

---

## 项目结构

```text
seewo_robot/
├── main.py              # [路径A] 主程序：消息常驻监听 + 命令处理 + 自适应轮询 + 断线重连
├── send_msg.py          # [路径A] 一次性快速发送文本（直连希沃）
├── upload_file.py       # [路径A] 一次性上传媒体文件到希沃云存储（只上传不自动发送；直连希沃）
│
├── auto_attend.py       # [独立脚本] 批量考勤签到工具（不使用项目统一登录；次要功能，维护优先级低）
│
├── api_server.py        # [路径B] REST API 服务端（Flask）
├── tui_client.py        # [路径B] TUI 终端图形客户端（Textual）
├── client.py            # [路径B] CLI 命令行客户端 + Python SDK
│
├── mock_server.py       # [调试] 本地 Mock 服务器（跨两条路径）
│
├── message_service.py   # [核心层] 消息数据源层：chat_history.json 读写 + 内存缓存 + 多页聚合
├── request_manager.py   # [核心层] 统一请求层：全局节流 + HTTP 429 退避重试
│
├── login.py             # [共享] 登录模块（扫码、校验 Token、账户对象）
├── msg.py               # [共享] 消息收发 DAO
├── stu.py               # [共享] 学生信息管理 DAO
├── upload.py            # [共享] 媒体文件上传：通过希沃云存储接口推文件，返回 downloadUrl
├── yunban.py            # [共享] 云班扩展功能（班级列表、考勤、离线验证码等；仅 getpass 被聊天命令间接调用；次要功能维护优先级低）
├── api.py               # [共享] m-campus 统一 API 网关（pxencode/pxdecode 编解码）
├── funcs.py             # [共享] 工具函数：文件读写、聊天记录加载/合并、日志、px 编解码
├── init.py              # [共享] 全局初始化：读取 config.json、URL 集合、Mock 切换
├── qrcode.py            # [共享] 终端二维码渲染
│
├── test/                # [测试] 测试脚本目录（test_api.py 等）
│
├── pyproject.toml       # [元] 项目元数据与依赖声明（仅在此声明依赖）
├── uv.lock              # [元] uv 依赖锁定版本
├── requirements.txt     # [元] 用做锁文件（生成产物，勿手改；pip install -r 用）
├── config.json.example  # [配置] 配置文件示例
│
├── AGENTS.md            # 🤖 给 AI 助手的完整项目上下文 + 开发规范
└── README.md            # 📖 你正在读的这份文档
```

---

## 常见问题 / 排错指南

### Q：客户端连不上服务端？

1. 确认 `api_server.py` 已启动
2. 核对 `config.json` 的 `api_port`（默认 **5001**，不是 5000）
3. 确认 `api_key` 在服务端配置和客户端传入的一致

### Q：Token 过期了，怎么重新登录？

- **路径 A**：删除 `tokens.json`，重新运行脚本即可弹出二维码
- **路径 B**：TUI 客户端会自动引导；或手动调 `/api/login/qrcode` 拿到 Base64 二维码图片，微信扫码后轮询 `/api/login/status` 直到 `ok`

### Q：HTTP 429 Too Many Requests？

- `request_manager.py` 内置了全局节流（最少间隔 0.5 秒）+ 429 指数退避重试 3 次
- 如果频繁触发，全量同步时可把 `/api/sync_all` 的 `delay` 参数调大一些（默认 2 秒）

---

## 维护状态

本项目的维护精力**聚焦在聊天主流程**上：即路径 A（`main.py` / `send_msg.py` / `upload_file.py` 的上传发送链路）、路径 B（`api_server.py` / `tui_client.py` / `client.py` 及它们依赖的消息层、上传层、统一请求层、Mock 服务器等）。

以下功能属于**边缘次要工具**，维护优先级低，**可能存在导入问题、缺失依赖、缺乏实机测试等情况**，使用时请自行排查：

- 签到 / 考勤类批量工具（如 `auto_attend.py`）
- 云班扩展模块 `yunban.py` 中与聊天无关的班级/考勤管理功能

---

## 设计限制（当前没有的功能）

项目是围绕**单家长账号、单学生**场景设计的，数据文件与内存对象都以扁平形式存储，**没有任何多家长账号切换、或同一家长下多学生切换的机制**。

具体表现：

- **磁盘数据文件不分命名空间**：`chat_history.json`、`uploads.json` 等不按家长 UID / 学生 UID 区分。切换家长账号后，老账号的数据会和新账号的混在一起。
- **轮询 `msg_id` 推断会错**：main.py 用全局 `max(id)` 判断新消息，切到另一个家长账号后如果该账号的消息 id 较小，会被当作「已处理」而漏掉。
- **Mock 的接管机制不支持第二账号**：`adopt_parent` 只在首次遇到未知 UID 时迁移一次预置消息，第二个家长账号将拿不到预设数据。

如果你需要切换账号使用，请在重新扫码前手动备份或清理对应的数据文件。

---

## 已知问题

- ⚠️ **uploads.json 按纯文件名做 key，同名覆盖**：写入逻辑在 [upload.py#L121-L122](upload.py#L121-L122)，key 只取 `os.path.basename(file)`，不区分完整路径、不区分家长账号。同一个 basename 的文件重复上传（哪怕路径不同、账号不同、内容不同），后写的都会直接覆盖前一条。**影响范围有限**：主业务链路（上传 → 发送留言）走内存中的 `downloadUrl`，不读这份台账，因此不会导致发错、漏发；只会影响"事后手动打开 uploads.json 按文件名翻历史上传 URL"的查询场景。

---

## 希沃 API 列表

详见[Seewo-API](https://github.com/cuitepiglin/seewo-api)
