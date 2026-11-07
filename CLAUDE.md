# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目是什么

希沃云班「亲情留言」聊天机器人：家长身份收发留言、执行命令、上传媒体、查询考勤。

`AGENTS.md` 是本仓库的**完整项目上下文**（47KB，含状态码约定、协议细节、已知问题、数据文件清单）。本文件只保留 Claude Code 工作时最需要速览的部分；细节问题先查 `AGENTS.md`，不要凭记忆猜。

## 命令

依赖真源是 `pyproject.toml`；`requirements.txt` 是 pip-compile 生成的**锁文件（产物）**，不可手改。

```bash
.venv/bin/python -m pytest -q                        # 全量（696 passed，全程离线）
.venv/bin/python -m pytest tests/test_core_service.py -q      # 单文件
.venv/bin/python -m pytest tests/test_core_service.py::TestDeduplication -q   # 单个类
.venv/bin/python -m pytest -q -k "capability"          # 按名字筛选

.venv/bin/python -m ruff check .
.venv/bin/python -m ruff format .
.venv/bin/python -m ruff format --check .
```

本机已装好 `.venv`（Python 3.12），**直接用它**。装依赖：`pip install -e ".[dev]"`。`uv sync --extra dev` 亦可，但本机**没有 uv**。

**pre-commit 钩子**（激活一次：`git config core.hooksPath .githooks`）会对暂存的 `.py` 跑 `ruff format` 并重新暂存；`pyproject.toml` 变更时用 `pip-compile` 增量更新 `requirements.txt`。**缺 ruff / pip-compile 会阻止提交**，不要用 `--no-verify` 绕过。

跑 mock 调试（不连真实希沃）：`config.json` 设 `use_mock=true`，先 `.venv/bin/python mock_server.py` 再 `.venv/bin/python api_server.py`。

## 两条独立运行路径

这是本仓库**最高优先级的结构约束**：两条路径的**会话对象各自独立**（路径 A 全局 `account/student/stu_msg`；路径 B 的 `Session` 对象），但**共享磁盘状态文件**（`tokens.json` / `chat_history.json` / `config.json` / `uploads.json` / `logs/`）。

- **路径 A（独立脚本，直连希沃）**：`main.py`（常驻轮询 + `/` 开头命令 + 自适应间隔 + 断线重连）、`send_msg.py`、`upload_file.py`、`yunban_*.py`、`auto_attend.py`。
- **路径 B（客户端-服务端，HTTP 中转）**：`api_server.py` + `client.py` / `tui_client.py`。路径 B 的客户端**只**经 HTTP 通信，本身不登录、不接触希沃。

给路径 A 改东西时注意它不经过 `api_server` 的任何封装；给路径 B 改东西时注意 v1 端点的行为不能变。

## 架构：v1 现状与 v2 内核并存

仓库里同时存在**两套架构**，这是理解本项目最容易踩的坑。

### v1（路径 B 现状）

```
api_server.py (handler 薄层)
  → message_service.py (独占 chat_history.json + 格式化 + 多页聚合)
    → msg.py / stu.py / upload.py / yunban.py (单次 DAO)
      → api.py (m-campus 网关, pxencode/pxdecode)
        → request_manager.py (节流 + 429 退避)
          → models.py (根目录，进程内 dataclass)
```

`models.py` 在**仓库根目录**，不是 `seewo/models.py` —— 两者是不同的东西，import 时注意。

### v2（`seewo/` 包）

平台中立的通用消息服务内核，与 v1 **完全并存**（v1 端点行为不变）。依赖**严格单向向下**：

```
interfaces/ (REST v2, OneBot, Satori, SDK)
  → integrations/ (OneBot 11 / Satori 协议翻译)
    → core/ (去重/排序/游标/并发/状态/事件)
      → provider/ (平台接入契约)
        → providers/ (希沃实现)
          → models/ + errors/ (平台中立, 纯标准库)
```

唯一例外：`seewo/providers/seewo.py` 被允许 import 根目录的希沃协议层（`api`/`login`/`msg`/`stu`/`upload`）—— **复用既有实现，刻意选择**。

`tests/test_layering.py` 用 AST 静态解析 import 把上述约定变成可执行检查（依赖方向、平台专有模块隔离、无逆向依赖、无 TODO、文件数上限）。**改依赖结构时它会告诉你哪里越界了。**

## v2 的关键不变量

1. `seewo/models.py` 与 `seewo/errors.py` **只依赖标准库**
2. `Provider.send()` 是**模板方法** —— 能力闸门（`ensure_can_send` + `ensure_text_length`）在 `send()` 内部强制生效，子类只实现 `_send()`。**不要覆写 `send()`**，那会绕过闸门
3. 平台没有的能力 → `capabilities` 申报 `False` + 抛 `CapabilityError`，**不伪造、不静默降级**
4. 事件派发必须串行（`_dispatch_lock`），保证事件序 == 消息序
5. 平台专有符号（`resUrl` / 数字 `type` / `statusCode`）只能出现在 `seewo/providers/seewo.py`；跨平台语义只看 `role` / `segments` / `platform`
6. 平台细节只进 `Segment.data` 与 `Message.native`
7. v2 **不读写** v1 的 `chat_history.json`：v2 用 `chat_history.v2.json`，`JsonStore` 遇到 v1 结构拒绝加载
8. v2 与协议端点复用 `config.api_key` 鉴权（getter 实时读取，热重载即时生效）

## 扩展方式

- **接新平台** → 实现 `Provider` 子类（`poll` + `_send` + `capabilities`）。核心层、接口层、集成层零改动。
- **接新协议** → 实现 `IntegrationServer` 子类（`translate_event` + `send_payload`），加一个 Server 注册 HTTP/WS 路由。
- **加新能力** → 先确认平台**真的支持**，再改 `Provider.capabilities`；涉及新段类型时 `seewo/models.py` 三处要同步（`SegmentType` / `supports_segment()` / `CAPABILITY_FIELD_BY_SEGMENT`）。

## 协议端点

服务启动时自动挂载，与 v1/v2 **共用同一个端口**：

| 路径 | 协议 | 接入方 |
| --- | --- | --- |
| `/onebot/v11/<action>` + `/onebot/v11/ws` | OneBot 11 HTTP + 正向 WS | NoneBot2 / AstrBot / NapCat |
| `/satori/v1/<resource>.<method>` + `/v1/events` | Satori HTTP + 事件 WS | Koishi / LangBot |

`api_server.py` 末尾用 `flask_sock.Sock(app)` 挂 WS 端点（flask-sock 以蓝图方式注册路由）。`tests/test_protocol_endpoints.py` 校验所有端点**真的挂上了** —— 防「适配器写好了但 WS 从未 attach」这类回归。

鉴权：OneBot 11 / Satori 端点复用 `config.api_key` 鉴权（每次请求实时读取，热重载即时生效）：OneBot 用 `Authorization: Bearer` 或 `?access_token=`，Satori HTTP 用 `Authorization: Bearer`，Satori WS 在 IDENTIFY 通过前不推送事件。

## 关键数据约定

- **消息列表顺序稳定为旧→新**（按 `id` 升序）。客户端显示必须按返回顺序。
- **希沃业务码只有一处定义**：`models.SeewoCode`。判定走谓词 `is_ok()` / `is_relogin_code()` / `is_business_reject()`，不要自己比较数字。
- **三套码禁止混用**：希沃业务码（`SeewoCode`）/ 扫码登录码（`SeewoQrCode`）/ HTTP 状态码。`SendResult` 里 `seewo_code` 与 `http_status` 分开存放。
- `-500/-505`（Token 失效）→ 刷新会话后重发**一次**，仍失效则 401 `need_login`；`40000`（业务校验失败）→ **不重试**。
- **199 字截断不可移除**：希沃服务器对单条留言强制 200 字上限（超长返回 `statusCode=40000`），`196 + "..." = 199` 是客户端镜像。移除会导致消息静默丢失。已于 2026-08-18 真机实测确认。
- **配置读后即过期**：`init.config` 是可变模型，从 `config.<字段>` 读出的值不得缓存复用，否则热重载不生效。
- **不得硬编码状态文件名**：一律用 `init.project_path()` / `funcs.chat_log_file()`。`chat_log_file()` 每次调用即时按 `config.use_mock` 返回路径，**不要缓存成模块常量**。

## 写测试的纪律

断言要落在**公开契约**上，不要把当前实现的偶然细节固化成预期。

- **不要触碰私有成员**（`_messages` / `_refresh` 这类下划线开头的东西）。用公开方法观察行为。`tests/test_message_service.py` 曾有这个毛病，已按 `docs/TEST_REVIEW.md` 的审查结论整改 —— **新增测试不要重复该模式**。
- **不要用 `pytest.raises(Exception)`** —— 盲捕异常，任何异常都能通过。要断言具体异常类型，必要时连异常属性一起断言（如 `exc.value.capability`）。
- 能力相关的用例要写**正反两面**：可用时行为正确 + 不可用时抛 `CapabilityError`（而不是静默成功或静默失败）。
- 协议适配层用 `FakeProvider`（`seewo/testing/fake_provider.py`）测试，**不连真实希沃**。
- 契约测试（`tests/test_provider_contract.py`）新增断言时，要**同时对 `FakeProvider` 和 `SeewoProvider` 成立**。
- 断言要有约束力：检查过 `continue` 之后的 `assert` 是否真的可达，别留下空断言。
- 并发用例用**真实时钟**而非 mock，断言实际串行化效果而不是「调用了锁」。

`testpaths = ["tests"]`；`test/` 下是手工联调脚本（`test/test_api.py`），**不是** pytest 用例 —— 它需要**活的 api_server**（发送类端点会跳过以免误发），不要试图在 CI 里跑它。

## Git 工作流

- Conventional Commits 风格，中文 body。
- **用户要求审阅 commit message 之前不要执行 commit。**

## 已知问题（不要试图"顺手修掉"，除非用户要求）

- `auto_attend.py` 粗糙非生产级：硬编码 `classlist[35]`，事件用 `events[1]` 无兜底会 `IndexError` 崩溃。
- `yunban.py` 直接 `requests.request` **绕过** `request_manager`：无统一节流 / 429 退避，批量取数需调用方自行 `time.sleep`。
- 聊天记录损坏 = **启动即失败**（故意不吞异常）：读容错 + 全量覆盖写会把损坏当空记录，一次 append 就把全部历史覆盖成 1 条。
- `ruff BLE001` 暂时全局忽略（约 38 处 `except Exception` 待逐点收窄），收窄完成后删掉 `pyproject.toml` 里的 `ignore`。
- WS 端点依赖 Werkzeug 开发服务器；生产部署需换支持 WS 的 WSGI 服务器。
- 希沃协议能力缺口（撤回 / 提及 / 引用 / 视频与文件 / 流式）**是协议限制，不是待办**，不要猜测协议行为。清单见 `docs/CAPABILITIES.md`。
- v2 / OneBot / Satori **不能发媒体**（未接上传器，避免远程调用方借其上传服务端任意文件）；媒体走 v1 端点。
- v2 **没有后台轮询**：入站消息由外部调用 `/api/v2/sync` 或 `GET /api/v2/messages` 触发。

## 相关文档

| 文档 | 内容 |
| --- | --- |
| `AGENTS.md` | 完整项目上下文 —— 状态码约定、协议细节、各模块职责、已知问题 |
| `docs/ARCHITECTURE.md` | `seewo/` 分层、依赖方向、数据流、不变量清单 |
| `docs/CAPABILITIES.md` | 平台能力声明与已知能力缺口 |
| `docs/INTEGRATION.md` | OneBot 11 / Satori 接入实操、自写适配器指南 |
| `docs/MIGRATION.md` | v1 → v2 迁移指南、兼容性保证 |
| `docs/TEST_REVIEW.md` | 测试审查报告（断言落在需求还是实现的判据） |
| `README.md` | 用户向使用文档 |
