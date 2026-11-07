# 测试审查报告

> 审查对象：`tests/` 下的单元测试。
> 审查标准：测试断言的是**被测对象的真实需求 / 公开契约**，而不是**当前实现的偶然细节**。
> 重点排查：AI（或人）根据现有实现反推出来的、把实现细节甚至缺陷固化成"预期行为"的测试。

审查日期：2026-11-06
审查范围：`tests/` 全部 21 个文件、约 600 个用例。

---

## 0. 结论摘要

| 分类 | 数量 | 说明 |
| --- | --- | --- |
| ✅ 有需求/文档依据 | 绝大多数 | 断言落在公开契约、业务码语义、HTTP 契约、协议规范上 |
| ⚠️ 可疑（断言私有状态） | 1 个文件，约 20 处 | `test_message_service.py` 全线触碰私有成员 —— **已修复** |
| ⚠️ 可疑（断言实现细节） | 3 处 | 见 §3 —— **已修复** |
| ❌ 把缺陷固化成需求 | **0 处** | 没有发现把明确的 bug 写成预期行为的情况 |

**总体判断**：仓库既有测试的**质量高于平均**。绝大多数用例都能在 `AGENTS.md` / `README.md` / 协议文档中找到依据。真正的问题集中在**一处系统性的风格问题**（触碰私有状态）和**少数几处**把实现细节写死。

> **修复状态（2026-11-06）**：本报告提出的 P2/P3 三项**已全部落地修复**，
> 未改动任何生产代码行为（纯测试层改动）。修复明细见 §6。

---

## 1. 有明确需求依据的（无需改动）

以下类别的断言都能追溯到文档或公开契约，**不应改动**：

| 类别 | 例子 | 依据 |
| --- | --- | --- |
| 希沃业务码语义 | `test_token_invalid_triggers_relogin_and_resend`、`test_business_error_is_not_retried` | `AGENTS.md`「状态码约定（三套码，禁止混用）」明确 `-500/-505` 刷新重发一次、`40000` 不重试 |
| 长消息 199 上限 | `test_max_len_constant_keeps_room_for_ellipsis` | `AGENTS.md`「199 字截断是服务器约束的客户端镜像，不可移除」—— 有 2026-08-18 真机实测记录 |
| 拆分的无损性 | `test_content_is_lossless`、`test_newline_moves_to_next_chunk_without_loss` | AGENTS.md 明确「匹配字符整体跟到下一段开头不丢失」 |
| 合并不去重 | `test_does_not_deduplicate` | `AGENTS.md`「`merge_messages` **不去重**（同 id 重复传入会重复落盘，调用方自行保证）」—— 有明确声明 |
| `type` 字段 `int\|str` 容忍 | `test_keeps_msg_type_as_given` | `AGENTS.md`「保留 main.py 写 `str` / message_service 写 int 的既有混合行为」 |
| 损坏文件启动即失败 | `test_broken_json_raises` | `AGENTS.md`「聊天记录损坏 = 启动即失败（已修，故意不吞异常）」—— 有实测复现记录 |
| 未实现能力明确报错 | `test_add_stu_is_explicitly_not_implemented` | 需求就是「不伪装成成功」，测试断言 `NotImplementedError` 正是契约 |
| 请求层节流 | `test_serializes_concurrent_callers`、`test_retries_after_429_with_exponential_backoff` | `AGENTS.md`「全局节流 + 429 指数退避重试（3 次）」 |
| 顺序约定 | 各处 `== [1, 2, 3]` 顺序断言 | `AGENTS.md`「所有消息列表顺序稳定为旧→新」 |

---

## 2. ⚠️ 系统性问题：`test_message_service.py` 全线触碰私有状态

### 实际断言了什么

该文件 35 个用例中约 20 处直接读写 `MessageDataSource` 的**私有成员**：

```python
assert list(ds._messages) == []  # 私有缓存 list
ds._refresh()  # 私有方法
ds._invalidate()  # 私有方法
formatted = ds._persist([raw_msg(1), raw_msg(2)])  # 私有方法
```

### 依据的是需求还是实现

**依据的是当前实现**。`_messages` / `_refresh` / `_invalidate` / `_persist` 都以下划线开头，是**明确的私有约定**。它们不是公开契约的一部分。

### 为什么可疑

1. **把「缓存存在」当成了需求**。`test_missing_file_yields_empty_cache` 断言 `ds._messages == []`。真正要保证的是「读历史时不会返回脏数据」—— 而缓存**存不存在、用什么结构存**，是实现选择。换成 `dict`、换成 `deque`、或干脆每次重读文件（性能换正确性），需求照样满足，但这些用例全部会红。

2. **把「缓存失效的触发方式」当成了需求**。`test_reloads_when_file_changes_externally` 显式调用 `ds._refresh()` 才能看到新数据。这锁定的是「必须显式调 `_refresh`」这个实现机制。真正要保证的是「**外部写入后能读到新数据**」。

   值得注意的是，`_refresh` 目前**已经**同时比对 mtime 与文件大小（本次改造补的），但**两者都相同且内容变了**时仍会漏判 —— 这是「感知外部写入」这一机制的固有边界（彻底解决需要文件锁或版本号）。用例显式调 `_refresh()` 恰好绕开了这条自动感知路径，所以**没有覆盖到这个真实边界**。改成断言「调用公开读接口能读到新数据」才能暴露它。

3. **让重构成本变高**。`api_server.py` 里 `local_bounds()` 就是为了替换对 `datasource._messages` 的跨层私有访问而加的 —— 说明**项目自己已经认定**「外部不该碰 `_messages`」，但测试还在碰。文档与测试的约定不一致。

### 建议

改成断言**公开行为**（经 `load_local()` / `fetch_latest()` 等公开方法观察），而不是内部状态：

```python
# 现状（断言实现细节）
assert list(ds._messages) == []

# 建议（断言公开契约）
assert ds.load_local()["messages"] == []
```

「显式 `_refresh()` 才生效」这一条尤其应当改写为「外部写入后调用公开读接口能读到新数据」，并让它**真正走自动感知路径**（不手动 `_refresh()`），这样才能暴露 mtime 精度那个真实缺陷。

### ✅ 已修复（2026-11-06）

该文件的 35 个用例已全部改为经**公开路径**断言：

| 原写法 | 现写法 |
| --- | --- |
| `assert list(ds._messages) == []` | `assert ds.load_local()["messages"] == []` |
| `ds._refresh()` | 直接走 `load_local()` / `fetch_latest()` 的自动感知路径 |
| `ds._invalidate()` | 写文件后调 `ds.load_local()`，断言读到了新数据 |
| `ds._persist([...])` | `ds.persist([...])`（该方法已提升为公开 API，见下） |

配套的生产代码改动只有一处：**`MessageDataSource._persist` 改名为公开的 `persist`**，
并在 docstring 里写明它是「把一批上游原始消息写进本地记录」的完整语义 ——
测试与外部调用方不应依赖私有命名。这是**提升可见性**，不是改变行为。

> **仍存在的已知边界**：自动感知路径比对 mtime **与** 文件大小；
> 两者都相同但内容变了（同一秒内原地覆写等长内容）仍会漏判。
> 这是「感知外部写入」这一机制的固有边界，彻底解决需要文件锁或版本号。
> 用例现在**覆盖到了**这条路径（不再靠手动 `_refresh()` 绕开），但当前实现不宣称能覆盖它。


---

## 3. ⚠️ 三处把实现细节写死的断言（均已修复）

### 3.1 `test_refresh.py::test_reuses_cache_when_mtime_unchanged`（`test_message_service.py`）

- **实际断言**：monkeypatch 掉 `funcs.load_chat_history` 并断言它**没被调用**。
- **依据**：当前实现的 mtime 缓存策略。
- **可疑点**：把「靠 mtime 判断是否重读」这一**缓存实现策略**固化成了需求。需求是「不要每秒重读整个文件」或「外部写入要能读到」—— 换成 size+mtime（本次改造已在 `MessageService` 里采用）、或换用文件锁、inode 变化检测，都会让这条用例变红，但需求仍然满足。
- **建议**：断言「连续两次读之间，底层文件读取次数不增加」这种**效果**，而不是「某个特定函数没被调」。
- **✅ 已修复**：改名为 `test_reuses_cache_when_file_unchanged`，断言的是**效果**（底层读盘次数不增加），不再绑定「靠 mtime 判断」这一具体策略。

### 3.2 `test_upload.py::test_second_upload_on_same_instance_is_skipped`

- **实际断言**：同一个 `Upload` 实例第二次 `upload()` 返回 `None` 且**不发出第二次 HTTP 请求**。
- **依据**：`Upload` 实例有 `isupload` 一次性标志 —— 纯实现细节。
- **可疑点**：这实际上固化了一个**限制**（一个实例只能上传一次）。若将来支持批量上传或并发上传，这条会挡路。而**公开函数** `upload_file(account, file, type)` 每次都新建实例，本来就没有这个限制 —— 所以这条断言保护的是一个**非公开用法**的行为。
- **建议**：保留但降级为「实例级一次性语义」的说明性用例，或改为断言公开函数 `upload_file()` 连续调用两次会发出两次请求（即公开契约确实没有这个限制）。
- **✅ 已修复**：保留原用例但**降级为说明性用例**（docstring 明确它是实例级行为、非公开契约），并**新增** `test_public_entrypoint_uploads_each_call` —— 断言公开函数 `upload_file()` 连续调用两次会真的发出两次 HTTP 请求。两条一起把「实例限制」与「公开契约」区分开，不再让公开用法被实现细节绑住。

### 3.3 `test_funcs.py::test_overwrite_chat_history_file_format`

- **实际断言**：`raw.endswith("}") and "\n" in raw  # indent=2`。
- **依据**：当前写文件用的 `json.dump(..., indent=2, ensure_ascii=False)`。
- **可疑点**：**缩进与换行是纯格式选择**，不是需求。需求只是「写成合法 JSON、含 `messages` 键、内容不转义」。断言 `indent=2` 会无谓地阻止格式化器改动。
- **建议**：断言 `json.loads(raw)` 可解析、含 `messages` 键、`ensure_ascii=False`（中文不转义 —— 这一条**是**需求，因为它影响文件可读性与体积）。
- **✅ 已修复**：改为断言「能 `json.loads` 解析 + 含 `messages` 键 + 内容正确 + 中文未转义」，**不再断言 `indent=2` / 结尾换行**。格式化器此后可自由调整缩进。

---

## 4. 新增测试的自查（`seewo/` 相关 6 个文件）

按同一标准审查本次新增的测试：

| 文件 | 审查结论 |
| --- | --- |
| `test_models_v2.py` | ✅ 断言的是模型公开语义（`to_dict`/`from_dict` 往返、能力表、跃迁表）。**注意**：`test_segment_is_frozen` 断言 `dataclasses.FrozenInstanceError` —— 冻结是**明确的设计决策**（模型不可变），有依据 |
| `test_core_service.py` | 绝大多数通过公开方法（`ingest`/`poll_once`/`send`/`history`/`on_any`）观察行为；少量既有用例仍触碰 `_seen` 等私有成员，属待整改项。反例（能力缺口、认证失效）都有 |
| `test_integrations.py` | ✅ 断言的是 **OneBot 11 / Satori 协议规范**（字段名、retcode、opcode、元素语法），这些是**外部公开标准**，不是本项目实现 |
| `test_rest_v2.py` | ✅ 断言 HTTP 契约（状态码、响应信封）+ SDK 与 REST 的一致性 |
| `test_provider_contract.py` | ✅ **同一套断言跑在 `FakeProvider` 与 `SeewoProvider` 上** —— 断言的是 Provider **契约**，不是任一实现的细节。这是本次改造最值得保留的设计 |
| `test_layering.py` | ✅ 断言架构约束（依赖方向），是**可执行的架构契约** |
| `test_protocol_endpoints.py` | ✅ 断言端点**真的挂上了** —— 防「适配器写好了但 WS 从未 attach」这类回归 |

### 审查中发现并已修正的两处自身问题

1. **`test_send_without_uploader_raises` 断言 `pytest.raises(Exception)`** —— 盲捕异常，任何异常都能通过。已改为断言 `CapabilityError` 且检查 `exc.value.capability == "send_image"`，把「不伪造能力」这条需求明确化。

2. **`test_no_circular_imports_in_seewo_package` 是一条空断言** —— 原实现里 `continue` 之后的 `assert` 永远不可达，且 `layers` 变量是死代码。已重写为按层号表检查「是否存在逆向（向上）依赖」，现在真的有约束力。

---

## 5. 值得推广的做法（既有测试里做对的）

这些是仓库测试的亮点，新测试应保持：

1. **能力缺口的正反两面**：`test_send_msg_rejects_group` 断言的是「明确失败 + retcode=100 + 没有发出消息」，三个维度一起断言 —— 既验证了「不静默降级」，也验证了「不假装成功」。

2. **废弃 API 用 `pytest.warns(DeprecationWarning)` 锁定**：`test_get_content_warns_deprecated` / `test_get_last_warns_deprecated`。这让「这个 API 已废弃但先不删」变成了可执行的契约。

3. **异常消息也纳入断言**：`pytest.raises(KeyError, match="'u9'.*u1.*u2")` —— 错误信息里带上候选值是可诊断性需求，值得锁住。

4. **并发测试用真实时钟而非 mock**：`test_serializes_concurrent_callers` 断言真实耗时 ≥ 0.1s，验证的是**实际串行化效果**而不是「调用了锁」。

5. **`test_source_file_not_locked_after_failed_upload`**：断言「失败后源文件可删」—— 这是**资源清理**的真实需求，用 `os.remove` 能否成功来观察，比 mock 掉 `open` 更贴近实际。

---

## 6. 行动建议（已执行）

| 优先级 | 建议 | 状态 | 影响 |
| --- | --- | --- | --- |
| P2 | `test_message_service.py` 改为断言公开行为 | ✅ 已完成 | 纯测试风格，不改功能。降低重构成本，并让 mtime 精度缺陷暴露出来 |
| P3 | 放松 `test_overwrite_chat_history_file_format` 的 `indent=2` 断言 | ✅ 已完成 | 允许格式化器自由调整 |
| P3 | `test_second_upload_on_same_instance_is_skipped` 降级为说明性用例 | ✅ 已完成 | 避免挡住未来的批量上传能力 |
| — | 新增测试**不得**触碰私有成员 | ✅ 已写明 | 已在 `AGENTS.md` 的「写测试的纪律」中写明 |

**执行结果**：三项全部落地，**生产代码行为零变更**（唯一的代码改动是把
`MessageDataSource._persist` 提升为公开的 `persist`，属可见性调整）。
全量测试与 lint 在修复后重跑通过。

> 后续纪律（已写入 `AGENTS.md`「写测试的纪律」）：
> 新增测试**不得**触碰私有成员、不得用 `pytest.raises(Exception)` 盲捕异常、
> 断言必须落在**公开契约**或**可追溯的需求**上。

---

## 7. 相关文档

- [`../AGENTS.md`](../AGENTS.md) —— 「写测试的纪律」一节
- [`ARCHITECTURE.md`](ARCHITECTURE.md) —— 架构不变量
