# 飞书 ↔ Claude Code 会话桥：设计与实施指南（A 模式 / Python）

> **形态**：飞书机器人在任意时刻只驱动**一条** Claude Code 会话（全局单活跃）。会话自带固定工作目录；用 `/sessions` 看列表、`/use` 切到另一条。
> **原则**：薄桥接。不做 Super-agent、不做多 Ticket、不**自动**新建会话（`/new` 是显式命令，不在此列）。
> **技术栈**：Python 3.12（conda 环境 `oncall`）/ `claude-agent-sdk 0.2.178` / `lark-oapi 1.7.5` 长连接。
> 修订：2026-11-04。本文所有 API 签名**已在本机实测确认**，见第 2 章。

---

## 1. 目标与边界

做什么：

- 在飞书里发一句话 → 本机某条已存在的 Claude Code 会话在它自己的仓库里继续干活 → 过程流式回到飞书卡片。
- 会话可切换，但同时只有一条活跃。`/use` 只在已有会话里挑，`/new` 显式新建一条。

**主要用法是私聊机器人**（`chat_type == "p2p"`）：不建群、不需要 @、不需要话题。群聊也支持，但要 @ 机器人。两者走同一条主流程，差别只在 `policy.require_mention_in_group` 这一道闸门。

私聊的一个后果要记在心里：没有 @ 这道闸门，**发出去的每一句非命令消息都会驱动 Claude**。队列和 `/stop` 是唯一的缓冲。

不做什么（第一版）：

- **不自动新建会话**。会话文件没了就报错，让人重新 `/use` 或 `/new`，绝不静默新建一条顶上。
  注意这条约束说的是「自动」：`/new` 是用户显式下的命令，不违反它。
- 不做多群并行（B 模式）。全局一条，谁发消息都驱动同一条会话，输出回到发消息的那个会话窗口。
- 不做 ask 模式审批卡片（见第 12 章 v2）。第一版只有 `trusted`，靠工具黑名单兜底。
- 支持文字、图片、文件、富文本（`post`）输入，见 9.5。语音 / 视频 / 表情等其余类型明确回「暂不支持这种消息」。
- 不做多机部署。桥必须跑在会话所在的这台机器上。

---

## 2. 已实测确认的事实（实现时可以直接依赖）

在 conda 环境 `oncall`（Python 3.12）下安装 `claude-agent-sdk 0.2.178` + `lark-oapi 1.7.5`，实测结论：

### 2.1 resume 语义 —— 整个方案的立足点

用三个**独立**的 `ClaudeSDKClient`（即每轮新建、结束即断，正是本方案的进程模型）连续跑三轮：

| 轮次 | `resume` 入参 | `ResultMessage.session_id` | 回复 |
|------|---------------|----------------------------|------|
| 1（新建） | `None` | `a85a1b7e-…` | 「已记住」 |
| 2（续） | `a85a1b7e-…` | `a85a1b7e-…` | 「紫色河马七号」 |
| 3（续） | `a85a1b7e-…` | `a85a1b7e-…` | 「紫色河马七号」 |

**结论：`resume=<id>` 且 `fork_session=False`（默认）时，session_id 保持不变，上下文完整延续。** 不会 fork 出新会话。测试会话事后已用 `delete_session` 删除。

### 2.2 Claude Agent SDK 可用面

- `list_sessions(directory=None, limit=None, offset=0, include_worktrees=True) -> list[SDKSessionInfo]`
  **`directory=None` 时返回全机所有项目的会话**，这正是 `/sessions` 需要的。
- `get_session_info(session_id, directory=None) -> SDKSessionInfo | None`
- `delete_session(session_id, directory=None)`、`rename_session(...)`、`tag_session(...)`
- `get_session_messages(session_id, directory=None, limit=None, offset=0) -> list[SessionMessage]`
  按 `parentUuid` 串好的消息链，每条带 `uuid`。配下面的 `fork_session` 用。
- `fork_session(session_id, directory=None, up_to_message_id=None, title=None) -> ForkSessionResult`
  **在指定消息处截断并复制成一条新会话**（UUID 全部重映射），原文件不动、不调模型。这是上下文撑满之后唯一的离线出路，见 2.10。
- `ClaudeSDKClient.get_context_usage()`：等价于 CLI 的 `/context`，给出 `totalTokens` `maxTokens` `rawMaxTokens` `percentage` `isAutoCompactEnabled` `autoCompactThreshold`，还有按 system prompt / tools / skills / messages 分类的占用。
- 客户端方法还有 `set_model` `set_permission_mode` `interrupt` `stop_task` `rewind_files` `get_mcp_status`；**没有** `compact()`。
- `SDKSessionInfo` 字段：`session_id` `summary` `last_modified`(ms) `file_size` `custom_title` `first_prompt` `git_branch` **`cwd`** `tag` `created_at`
  → **工作目录来自会话本身，用户不需要输路径。**
- `ClaudeAgentOptions` 确认存在的字段（本方案用到的）：`cwd` `resume` `session_id` `fork_session` `permission_mode` `allowed_tools` `disallowed_tools` `can_use_tool` `system_prompt` `max_turns` `max_budget_usd` `model` `env` `cli_path` `settings` `setting_sources` `stderr` `hooks` `include_partial_messages` `add_dirs`
  还有一对用于「从中途分叉」的：`resume_session_at`（只加载到某个 uuid 为止）+ `resume_drops_turn`（校验被丢弃的范围确实只属于那一轮，不符合就拒绝 resume 并抛 `ProcessError`）。
- `permission_mode` 取值：`default` `acceptEdits` `plan` `bypassPermissions` `dontAsk` `auto`
- `ResultMessage` 字段：`subtype` `duration_ms` `duration_api_ms` `is_error` `num_turns` `session_id` `stop_reason` `total_cost_usd` `usage` `result` `model_usage` `permission_denials` `errors` `api_error_status` `terminal_reason`
- 异常类型齐全：`CLINotFoundError` `CLIConnectionError` `ProcessError` `ResultError` `CLIJSONDecodeError`

### 2.3 连接复用的实测数据（`scripts/bench_startup.py`）

用极简 prompt（只要求回一个字）把模型思考时间压到最小，剩下的就是连接成本。

**167 KB 的小会话**：

| | 每轮墙钟时间 |
|---|---|
| 每轮新建 client | 连接中位数 4.03s（2.22–10.22）+ 对话 → 约 7.2s |
| 长连接复用 | 首轮含一次性连接，之后 **2.4s** |

**19.1 MB 的真实会话**（用 `fork_session` 复制一份测，不污染原会话）：

| | 每轮墙钟时间 |
|---|---|
| 每轮新建 client | 连接中位数 **7.98s**（7.28–11.33）+ 对话 → **11.75s** |
| 长连接复用 | 首轮 11.17s，之后 **1.62s** |

长连接首条消息延迟也在递减：1.09s → 0.23s → 0.06s。

**会话越大差距越大**，因为每次连接都要重新加载整个历史、重新初始化 MCP 和 skills。

### 2.4 长连接的三个前提（已实测）

1. `ClaudeSDKClient` 有显式 `connect()` / `disconnect()`，不必靠 `async with`。
2. 一个连接上连续 `query()` 多轮，`session_id` 不变、上下文延续。
3. **`interrupt()` 之后同一个连接不可复用**：实测中断后 `ResultMessage.terminal_reason` 是 `api_error`，再 `query()` 直接拿到 `API Error: 422`。所以 `/stop` 和超时中断之后必须丢弃连接、下一轮重连。

第 3 条还有个副作用：中断后的 `terminal_reason` 不是 `aborted_*`，所以判断「本轮是否被中断」要以自己记录的 `handle.abort_reason` 为准，不能只看 `terminal_reason`。

### 2.5 `receive_response()` 在长连接下会串轮 —— 必须自己做关联

**这是长连接改造引入的回归，线上真实踩到过**：用户让它推代码，卡片回的却是上一轮后台轮询的结果；下一条消息（「我看过了」）才拿到推代码的结果。

根因在 `receive_response()` 的实现 —— 它就是 `receive_messages()` 加一句「遇到**第一个** `ResultMessage` 就 return」，**没有任何轮次关联**：

```python
async for message in self.receive_messages():
    yield message
    if isinstance(message, ResultMessage):
        return
```

Claude 自己起的后台任务会在**轮外**产出消息，其中包括它自己的 `ResultMessage`。长连接下这些消息留在流里，下一轮一 `query()` 就立刻读到它，于是：本轮答案 = 上一轮后台任务的输出，本轮的真实结果被推迟一轮。旧的「每轮新建 client」不会有这个问题，因为残留随进程一起丢掉了。

实测抓到的真实序列（一次 `query()` 之后收到**两个** `ResultMessage`）：

| 顺序 | `origin` | `num_turns` | `result` | 归属 |
|------|----------|-------------|----------|------|
| 1 | `{'kind': 'task-notification', 'producer': 'session-task', ...}` | 3 | 已启动第二条 | 上一轮后台任务 |
| 2 | `None` | 1 | 好 | **本轮** |

所以判据是 `origin`：

- `origin` 为 `None` 或 `kind == 'human'` → 本轮的结果，**只有它能终止本轮**
- 其余 `kind`（`task-notification` / `channel` / `peer` / `coordinator` / `observer` / `auto-continuation` …）→ 不是终止条件

两个实现细节：

- `MessageOrigin` 是 **TypedDict**，实际拿到的是 `dict`，要用 `origin.get("kind")` 而不是属性访问。
- `AssistantMessage` **没有** `origin` 字段，所以后台链里的助手文本无法靠 origin 过滤。用一个开关：遇到 `Task*Message` 置位、遇到任意 `ResultMessage` 复位，期间的内容不进本轮正文。复位条件放宽到「任意 ResultMessage」是为了防卡死 —— 后台链万一不收尾，本轮的 `ResultMessage` 照样能终止。

### 2.6 本机模型与认证的真实情况（与旧版文档不同）

`~/.claude/settings.json` 的 `env` 段把 Claude Code 指向了第三方网关：

```
ANTHROPIC_BASE_URL  = https://api.deepseek.com/anthropic
ANTHROPIC_AUTH_TOKEN = <已设置>
ANTHROPIC_MODEL / *_SONNET_MODEL / *_OPUS_MODEL = deepseek-flash[1M]
ANTHROPIC_DEFAULT_HAIKU_MODEL = deepseek-v4-pro[1m]
```

由此得到三条实现约束：

1. **认证不需要配置。** SDK 启动的是本机 CLI，CLI 自己读 `settings.json`，网关和 token 自动继承。配置里**不要**再搞 `api_key | local_login` 二选一；只留一个可选的 `runner.env` 覆盖口。
2. **不要在卡片上显示费用。** `total_cost_usd` 是按 Anthropic 价目表估的（三个字的回复报 $0.077），和实际计费无关。改显示耗时 + token 数。同理 `max_budget_usd` 不是真闸门，默认不设。
3. **stderr 上会持续出现噪音**：`[claude-code:unrecognized_model]`、`claude.ai connectors are disabled…`。渲染层只能把 stderr 写日志，**不得**当错误贴到卡片上。

### 2.7 旧版文档的一处错误

旧文档第 11.3 节称「SDK 默认会加载目标仓库的 `.claude/` 配置」——**说反了**。Agent SDK 默认不加载任何 setting source，必须显式传 `setting_sources=["project"]` 才会加载仓库的 hooks / MCP / skills。本方案默认传 `["user", "project"]`（与终端里 `claude` 的行为一致），并在配置里给出开关。

### 2.8 飞书 SDK 可用面

- `lark.ws.Client(app_id, app_secret, log_level=…, event_handler=…, auto_reconnect=True)`
- `lark.EventDispatcherHandler.builder("", "")` 上有 `register_p2_im_message_receive_v1`、`register_p2_card_action_trigger`
- `lark_oapi.api.im.v1.P2ImMessageReceiveV1`；`lark_oapi.event.callback.model.p2_card_action_trigger` 下有 `P2CardActionTrigger` / `P2CardActionTriggerResponse` / `CallBackAction` / `CallBackContext` / `CallBackOperator` / `CallBackToast`
- `lark_oapi.ws.client` 有模块级 `loop` 属性（实测为 `ProactorEventLoop`）→ **第 8.1 节的「线程内换 loop」变通方法可行**
- 异步 OpenAPI：`client.im.v1.message.areply / apatch / acreate`
- `ReplyMessageRequestBody` 字段：`content` `msg_type` `reply_in_thread` `uuid`
- `PatchMessageRequestBody` 字段：只有 `content`（更新卡片 = 传完整卡片 JSON）
- `EventMessage` 字段：`message_id` `chat_id` `chat_type` `content` `message_type` `mentions` `thread_id` `parent_id` `root_id` `create_time` …
- `MentionEvent` 字段：`id` `key` `name` `mentioned_type` `tenant_key`
- 原生请求取机器人 open_id：`BaseRequest.builder().http_method(HttpMethod.GET).uri("/open-apis/bot/v3/info").token_types({AccessTokenType.TENANT}).build()` → `client.arequest(req)` → `json.loads(resp.raw.content)`
- `BaseRequest` builder 字段：`http_method` `uri` `token_types` `paths`(dict) `queries`(list[tuple[str,str]]) `headers` `body`

### 2.9 云文档相关可用面（实测确认）

| 能力 | 接口 | 形状 |
|------|------|------|
| 新版文档纯文本 | `client.docx.v1.document.araw_content` | `RawContentDocumentRequest.builder().document_id(token).lang(0)` → `resp.data.content`（str）。**会丢图片位置，只作退路** |
| 文档 block 树 | 原生请求 `/open-apis/docx/v1/documents/{id}/blocks?page_size=500&document_revision_id=-1` | 扁平列表 + `children` 可还原文档树 |
| 素材下载 | `client.drive.v1.media.adownload` | `DownloadMediaRequest.builder().file_token(token)`。**成功时 `resp.code` 是 None、`success()` 返回 False，必须看 `raw.status_code`** |
| 文档标题 | `client.docx.v1.document.aget` | `GetDocumentRequest.builder().document_id(token)` → `resp.data.document.title` |
| 文档块结构 | `client.docx.v1.document_block.list` | 需要更精细的结构时用 |
| 知识库节点 | **没有** `wiki.v2.space_node.get` | 必须走原生请求 `/open-apis/wiki/v2/spaces/get_node?token=&obj_type=wiki` → `data.node.{obj_type, obj_token, title}` |
| 消息里的附件 | `client.im.v1.message_resource.aget` | 第一版不用，留给文件附件支持 |
| 缺权限错误码 | HTTP 400 + `code 99991672` | 响应体 `msg` 里会列出可用的 scope 清单和申请链接，可以直接转给用户 |
| 资源级拒绝 | **HTTP 403 + 响应体完全为空** | 没有任何 `code`/`msg` 可读，只能靠 HTTP 状态判断 |

#### 素材下载的三层权限（实测踩完）

图片下载失败有三种完全不同的原因，提示语必须分开，否则用户照着改也改不对：

| 现象 | 原因 | 怎么改 |
|------|------|--------|
| HTTP 400 + `99991672` | 应用没开通 scope | 加 `drive:drive:readonly`（或 `docs:document.media:download`），**发布新版本并等审核通过** |
| **HTTP 403 + 空响应体** | scope 够了，但这篇文档不允许下载素材 | 文档「更多设置」里「可复制、打印、下载」常限制为「仅可编辑的人」，而机器人只有可阅读。要么把机器人提升为可编辑协作者，要么放宽该设置 |
| 正文也读不到 | 机器人对文档没有任何权限 | 加为协作者，或文档设为组织内可阅读 |

判断方法：`/open-apis/drive/v1/permissions/{token}/public` 能读到 `security_entity`，值为 `anyone_can_edit` 就意味着只有可编辑的人能下载。实测那篇文档正文读得到（`raw_content` 正常 6883 字）、图片和附件全部 403，就是这一种。

另外两个实现要点：

- `batch_get_tmp_download_url` 在这种情况下返回 `code: 0` 但 `tmp_download_urls: []`，**成功的返回码配空结果**，不能只看 code 判断。
- 这两种拒绝对整篇文档的每张图都一样，必须**第一张就 break**，否则 24 张图会发 24 次无用请求、刷 24 条一样的提示。其余零散失败要折叠成一条。

#### block_type 对照（对着真实文档逐个核对，不是查文档猜的）

| 值 | 含义 | 值 | 含义 |
|----|------|----|------|
| 1 | page（带 `children`，给出真正的文档顺序） | 14 | code |
| 2 | text | 19 | callout（容器） |
| 3–11 | heading1–heading9 | 22 | divider |
| 12 | bullet | 23 | file（文档内附件） |
| 13 | ordered（`style.sequence` 为数字=新列表起点，`"auto"`=延续） | 27 | image（`image.token`） |
| 17 | todo | 33 | view（容器） |

两个实测要点：

- **有序列表不能按「遇到非 ordered 块就重置」计数**。真实文档里列表项之间夹着文本块和图片块，那样算会让 1/2/3 全变成 1。要用飞书给的 `style.sequence`。
- **`raw_content` 和 block 数量不一致**。实测那篇文档 24 个 image block，`raw_content` 只有 23 个 `image.png` 占位符（第 24 张被包在 `view` 容器里直接跳过）。所以按序号替换占位符的方案必然错位，必须走 block 树。
| 电子表格 / 多维表格 | `client.sheets.v3` / `client.bitable.v1` | 第一版不支持 |
| 旧版文档 | `client.doc` **不存在** | 只能走 `drive.v1.export_task` 异步导出 |

`ClaudeAgentOptions.add_dirs: list[str | Path]` 存在 → 抓下来的文档可以放在仓库外，单独授权给 Claude 读。

### 2.10 上下文窗口会撞墙，而 `/compact` 救不了（2026-11-10 实测）

一条被桥长期 resume 的会话（10-15 用到 11-10，1218 个助手回合，transcript 9.7 MB）撞到了网关硬上限：

```
API Error: 400 This model's maximum context length is 1048576 tokens.
However, you requested 1072629 tokens (1040629 in the messages, 32000 in the completion).
```

实测到的几件事：

- **自动压缩在这条路上根本没生效。** 用桥同一套 options 新建一条会话，`get_context_usage()` 报 `isAutoCompactEnabled=True`、`autoCompactThreshold=967000`、`maxTokens = rawMaxTokens = 1000000`；但那条出事会话的 transcript 里 `isCompactSummary` / `compact_boundary` **各 0 次** —— 从头到尾一次都没压缩，上下文一路涨到 1,015,645。
- **`/compact` 这时也救不了**：压缩本身要把整段历史发给模型做摘要，请求同样超窗口，照样 400。死锁。
- transcript 里还留着终端跑过的 `Auto-compact window for deepseek-flash set to 850k tokens`，设了 850k 也没触发。这个开关在第三方网关 + 不被识别的模型上指望不上（日志里有 `[claude-code:unrecognized_model]`）。
- **唯一的离线出路是 `fork_session`**：在某条消息处截断、复制成新会话，不碰原文件、不调模型，所以不会再撞 400。砍掉尾部若干轮让上下文落回窗口内，新会话就能用，那时 `/compact` 也跑得动。
- 找砍点的办法：扫 transcript 里每个 assistant 条目的 `usage.input_tokens + cache_read_input_tokens + cache_creation_input_tokens`，那就是**该请求发出时的上下文水位**，挑最后一个落在目标以下的 `uuid` 作为 `up_to_message_id`。
- **注意 fork 保留的是前面、丢的是最近。** 但原始 transcript 一直在 `~/.claude/projects/<项目目录>/<id>.jsonl`，新会话需要细节时让它自己去读那个文件，比把内容塞回上下文更省。

桥这边暴露出的三个缺口（**尚未实现**，见 13）：

- 这个 400 是被 CLI 当成**助手文本**吐出来的：桥照原样渲染进卡片，`is_error` 没置位，**日志里一个字都没有**。应该识别它、换成明确的失败卡片并记 ERROR。
- `/status` 应该显示上下文水位（`get_context_usage()` 直接能读）。
- 接近上限时在卡片上主动提醒，而不是让人一路撞墙。

---

## 3. 架构

```
飞书用户 @机器人
  → lark.ws.Client 长连接（独立线程，内部自带 event loop，无需公网）
  → run_coroutine_threadsafe 投递到 asyncio 主循环
  → Policy：message_id 去重 / 群内 @ 校验（私聊跳过）/ open_id 白名单
  → 命令？ → /sessions /use /status /stop /help
  → 否则 → 单条 FIFO 队列（全局唯一 worker，严格串行）
  → Runner：ClaudeSDKClient(resume=active.session_id, cwd=active.workdir) 跑一轮
  → Renderer：SDK 消息 → 节流更新同一张飞书卡片
```

不得偏离的决策：

| 决策 | 结论 | 原因 |
|------|------|------|
| 调用方式 | `ClaudeSDKClient`，不拼 `claude -p` | 需要 `interrupt()` 和带类型的消息流 |
| 进程模型 | **一个长连接 `ClaudeSDKClient` 跨轮复用**，空闲超时才断开；中断 / 异常 / 切换会话时丢弃重连 | 每轮新建的连接成本是 8 秒量级，大会话上每轮 11.75s → 1.62s。见 2.3、2.4 |
| 并发 | 全局严格串行（单队列单 worker） | 两个进程同写一条会话会损坏历史 |
| 会话丢失 | 明确失败，提示 `/sessions` + `/use` 或 `/new`；**永不自动新建** | 产品核心约束。只有 `/new` 这条显式命令会新建 |
| 工作目录 | 取 `SDKSessionInfo.cwd`，不让用户输 | 已实测可用，少一类出错 |

---

## 4. 目录结构

```
on_call_bot/
├─ pyproject.toml
├─ config.example.yaml
├─ .gitignore                     # config.yaml / *.log / __pycache__
├─ README.md
├─ src/oncall_bot/
│  ├─ __main__.py                 # 入口
│  ├─ config.py                   # pydantic 配置模型 + 校验
│  ├─ logging_setup.py
│  ├─ app.py                      # 组装 + on_message / on_card_action
│  ├─ policy.py                   # 去重、群 @、白名单
│  ├─ state.py                    # ActiveSession + 原子持久化
│  ├─ commands.py                 # 斜杠命令
│  ├─ docs_fetch.py               # 飞书云文档链接 → 本地 md
│  ├─ attachments.py              # 消息里的图片 / 文件 → 本地文件 + 待处理队列
│  ├─ feishu/
│  │  ├─ ws.py                    # 长连接线程
│  │  ├─ api.py                   # areply / apatch / bot open_id
│  │  ├─ cards.py                 # 卡片 JSON 纯函数
│  │  └─ parser.py                # 事件 → IncomingMessage
│  └─ runner/
│     ├─ connection.py            # 跨轮复用的 Claude 连接
│     ├─ turn_stream.py           # 区分本轮消息与后台任务消息
│     ├─ dispatcher.py            # 单队列 + worker + /stop + 空闲断连
│     ├─ session_runner.py        # 跑一轮
│     └─ render.py                # SDK 消息 → 卡片文本 + 节流
└─ tests/                         # 纯函数单测（parser / cards / render / state）
```

---

## 5. 配置（`config.yaml`）

**字段表不写在这里。** 完整的字段 / 默认值 / 取值范围见 [README 第 3 节](../README.md#3-配置)，可直接拷贝的样例是 `config.example.yaml`。这份设计文档只记**为什么这么设计**——同一张表抄两遍必然有一份变成过期的那份，这个坑已经踩过一次。

分组和各组的职责：

| 分组 | 管什么 | 关键决定 |
|------|--------|----------|
| `feishu` | 应用凭据 | 两项都必填，占位符（`cli_xxx` 之类）要在校验期拒掉，别等到调接口才报错 |
| `policy` | 谁能用、能在哪儿用 | `allowed_open_ids` 留空 = 不限制（README 必须警告）；`allowed_workdir_roots` 留空 = `/new` 拒绝路径参数，这是安全默认，见 7.1 |
| `runner` | 怎么跑 Claude | `permission_mode` + `disallowed_tools` 是安全面；`connection_idle_seconds` 是连接复用的唯一开关（`0` = 永不断开），见 9.2 |
| `render` | 卡片长什么样 | 节流间隔、单卡容量、长回答拆几张，见 9.6 |
| `docs` | 云文档抓取 | 独立开关 + 独立目录，和 `attachments` 互不影响，见 9.4 |
| `attachments` | 消息里的图片 / 文件 | 同上，见 9.5 |
| 顶层 | 状态与日志落盘位置 | — |

`config.py` 的硬性要求：

- 启动时校验 `app_id` / `app_secret` 非空且不是占位符。
- 所有路径字段统一 `expanduser()`；`allowed_workdir_roots` 还要 `resolve()`，否则 `..` 能绕过白名单。
- 默认值也必须过校验器（`validate_default=True`），否则配置里不写 `inbox_dir` 时 `~` 不会被展开。
- 校验失败打印到底是哪个字段错，然后**退出码 2**；长连接不可恢复地断掉是**退出码 1**，区分开才能让守护进程只重启后者。
- 字段之间的约束就地收敛而不是报错：`result_inline_max > max_card_chars` 时按后者处理并记 warning。只想调小单卡容量的人不该被迫改两个值。

---

## 6. 状态

```python
@dataclass
class ActiveSession:
    session_id: str
    workdir: str              # 来自 SDKSessionInfo.cwd
    title: str                # custom_title or summary
    switched_by: str          # open_id
    switched_at: str          # ISO8601
    last_active_at: str | None
    pending_create: bool      # /new 刚建好、还没跑过第一轮，见 7.1
```

- 全局**至多一个** `ActiveSession`（可以为 `None` = 未选择）。
- 持久化到 `state_file`，结构 `{"version": 1, "active": {...} | null}`；写临时文件 + `os.replace` 原子替换；读写走同一个 `asyncio.Lock`。
- 另存 `last_card`（`{message_id, chat_id}`）：进程启动时若存在，把那张卡片刷成「桥已重启，上一轮状态未知」，避免卡片永远停在「处理中」。

---

## 7. 命令

| 命令 | 行为 |
|------|------|
| `/sessions` | `list_sessions(limit=session_list_limit)`，列出序号、会话 ID 前 8 位、标题、仓库路径、分支、最后活动时间。序号→ID 的映射存在内存里，供 `/use <n>` 用 |
| `/use <序号\|会话ID>` | 校验后切换活跃会话。序号仅在最近一次 `/sessions` 的结果内有效；ID 支持前缀（唯一匹配）和完整 36 位 |
| `/new [路径]` | 新建会话，见 7.1 |
| `/status` | 当前活跃会话（ID、标题、仓库、最后活动）、是否运行中、队列长度 |
| `/stop` | 中断当前轮并清空队列 |
| `/help` | 命令说明 |
| 其他 `/xxx` | 回「未知命令」，**不**转发给 Claude |

`/use` 校验（全部通过才切换）：

1. 运行中则拒绝，提示先 `/stop`。
2. 参数解析是确定的，不猜：完整 36 位 UUID → 直接用；1～2 位纯数字 → 序号（列表上限 50 条，序号不会超过两位）；其余 → 当 ID 前缀，至少 4 位十六进制，**只有唯一匹配才接受**，匹配到多条就列出候选让用户再选。
   （早期版本一律拒绝前缀，但 `/sessions` 列表显眼地印着前 8 位 ID，用户自然会去复制它 —— 显示一个东西又拒绝它是设计不一致。）
3. `get_session_info(sid)` 必须返回非 `None`。
4. `info.cwd` 必须非空且目录存在。
5. 成功后回复：仓库路径、分支、会话标题、ID 前 8 位，并提醒 **期间不要在终端里同时打开这条会话**。

### 7.1 `/new` 新建会话

`/new` 是**唯一**会新建会话的入口，必须由用户显式下命令。会话丢失时的失败路径绝不走这里。

两个实测事实决定了实现方式：

| 事实 | 后果 |
|------|------|
| `ClaudeAgentOptions(session_id=<uuid>)` **会被采用**，`ResultMessage.session_id` 就是传进去的值 | 桥可以自己生成 ID，不用等第一轮跑完才知道是哪条会话。状态、连接、卡片用的都是同一个 ID |
| 只 `connect()` 不 `query()`，**会话文件不落盘**（`get_session_info` 返回 `None`） | `/new` 之后那条会话还不存在，「会话必须存在」检查会把它误判成「会话已丢失」。需要 `pending_create` 标记跳过它 |

流程：

```
/new [路径]
  → 运行中则拒绝（先 /stop）
  → 定目录：无参数 = 当前活跃会话的仓库；有参数 = 走白名单校验
  → 关闭现有连接
  → session_id = uuid4()
  → 存成活跃会话，pending_create = True
  → 回复新 ID 和仓库，让用户直接发需求

第一条普通消息
  → pending_create 为真：跳过「会话必须存在」检查
  → options 用 session_id=<新 ID>，不用 resume=
  → 拿到 ResultMessage（说明文件已落盘）→ 清 pending_create，
     并用 get_session_info 把标题从「（新会话）」换成 Claude 生成的摘要

之后的消息
  → 正常 resume 路径
```

**目录白名单**（`policy.allowed_workdir_roots`）：

| 配置 | `/new` 无参 | `/new <路径>` |
|------|------------|--------------|
| 留空（默认） | 允许，用当前仓库 | **拒绝**，并提示怎么配 |
| 配了根目录列表 | 允许 | 允许，限这些根目录之下 |

安全默认的理由：`/new <任意路径>` 配上 `bypassPermissions`，等于让飞书上的人在本机任何目录启动 Claude 并直接执行工具。不带参数的 `/new` 不受限，因为它只沿用当前会话已有的仓库，没有扩大攻击面。

失败路径（跑第一轮之前就出错，比如 `CLINotFoundError`）要**保留** `pending_create`，这样下一条消息还能重试创建。只有拿到 `ResultMessage` 才算创建成功。

---

## 8. 飞书接入

### 8.1 长连接线程（`ws.py`）

`lark.ws.Client.start()` 阻塞且使用模块级 loop，必须放独立线程并在线程内替换 loop：

```python
def run() -> None:
    import lark_oapi.ws.client as ws_client
    ws_client.loop = asyncio.new_event_loop()
    asyncio.set_event_loop(ws_client.loop)
    lark.ws.Client(app_id, app_secret, event_handler=handler,
                   log_level=lark.LogLevel.INFO).start()
```

- 事件回调里**只做投递**：`asyncio.run_coroutine_threadsafe(app.on_message(data), main_loop)`，立即返回（飞书约 3 秒内要确认，否则重推）。
- 卡片回调返回 `P2CardActionTriggerResponse({"toast": {...}})`。
- 线程 `daemon=True`；主循环负责生命周期。

### 8.2 解析（`parser.py`）

```python
@dataclass(frozen=True)
class AttachmentRef:
    key: str                  # image_key 或 file_key
    kind: str = "image"       # "image" | "file"，决定下载时传给飞书的 type
    name: str = ""            # 文件消息带原始文件名；图片没有

@dataclass
class IncomingMessage:
    message_id: str
    chat_id: str
    chat_type: str            # "p2p" | "group"
    thread_id: str
    sender_open_id: str
    text: str
    mentioned_bot: bool
    attachments: list[AttachmentRef]
    # is_attachment_only：有附件但没文字 —— 这种只回执、不跑一轮，见 9.5
```

- `message.content` 是 JSON 串：`text` 取 `{"text":…}`；`image` 取 `image_key`；`file` 取 `file_key` + `file_name`；`post` 拼接文本段并收集其中的 `img` / `file` 标签；其余类型返回 `None`，由调用方回「暂不支持这种消息」。
- 删除 `@_user_1` 之类占位符；`mentioned_bot` 用 `mentions[*].id.open_id == bot_open_id` 判断。
- 启动时取机器人 open_id（见 2.8 原生请求写法）并缓存。

### 8.3 发送（`api.py`）

| 函数 | 实现 |
|------|------|
| `reply_text(message_id, text, in_thread)` | `areply`，`msg_type="text"` |
| `reply_card(message_id, card, in_thread) -> card_message_id` | `areply`，`msg_type="interactive"`，返回 `resp.data.message_id` |
| `patch_card(card_message_id, card)` | `apatch`，body 只有 `content` |
| `get_bot_open_id()` | `arequest` + `/open-apis/bot/v3/info` |

- 卡片 JSON 的 `config` 必须含 `"update_multi": true`，否则群内卡片无法更新。
- **正文元素用 `{"tag": "markdown", "content": …}`，不要用 `{"tag": "div", "text": {"tag": "lark_md"}}`**。实测结果见下表。
- 话题内消息回复时 `reply_in_thread=True`。
- 调用失败记日志并重试 1 次；仍失败则放弃本次更新，**不得**因此中断 Claude 的执行。

### 8.4 卡片（`cards.py`，纯函数）

| 卡片 | 内容 |
|------|------|
| 运行中 `running_card` | 标题「处理中 · <仓库名>」；正文累计文本 + 工具调用摘要行；底部「已运行 Ns · 发送 /stop 可中断」 |
| 完成 `done_card` | 标题「完成 · <仓库名>」；正文最终结果；底部耗时、轮数、token 数、会话 ID 前 8 位（**不显示费用**，见 2.6）。回答拆成多张时标题带 `（3/3）`，统计信息只出现在这最后一张上 |
| 续页 `part_card` | 多卡片里除最后一张以外的那些。标题带 `（1/3）`，脚注写「接着看第 2 张」，见 9.6 |
| 已中断 `aborted_card` | 标题「已中断」；已有输出 + 中断原因 + 「下一条消息会续上」 |
| 失败 `failed_card` | 标题「失败」；原因 + 下一步建议 |
| 状态未知 `stale_card` | 进程重启时把上次遗留的「处理中」卡片刷成这个，见第 6 节 `last_card` |

### 8.5 飞书卡片的 markdown 支持（实测，`scripts/check_card_markdown.py`）

发一张对照卡片到私聊，用实际渲染结果确认，不靠文档猜：

| 语法 | `div` + `lark_md` | `markdown` 组件 |
|------|-------------------|-----------------|
| 加粗 / 斜体 / 删除线 / 链接 | 支持 | 支持 |
| 代码围栏 ``` | **不支持**（原样显示反引号） | **支持，带行号** |
| 带语言的代码块 | 不支持 | **支持，带语法高亮** |
| 有序 / 无序列表、嵌套 | **不支持**（降级成纯文本） | **支持** |
| `---` 分割线 | 不支持 | 支持 |
| 标题 `#` | 不支持 | **不支持** |
| 行内代码 `` ` `` | 不支持 | **不支持** |
| 表格 | 不支持 | **不支持** |

所以：正文一律用 `markdown` 组件；它仍然不支持的三样在渲染层做等价替换（见 9.6）。

**行内强调写法的实测**（第二张对照卡片，`--inline`）：

| 写法 | 结果 |
|------|------|
| `` `x` `` | 裸反引号，不渲染 |
| `<font color='grey'>x</font>` | 标签被吃掉，**但没有颜色** —— grey 不在飞书调色板里 |
| `<font color='blue'>x</font>` | **渲染成蓝色，选这个** |
| `<font color='red'>x</font>` | 渲染成红色，但读起来像报错 |
| `**x**` | 渲染加粗，但会和模型自己的加粗混在一起 |
| `<u>x</u>`、`<code>x</code>` | 标签被吃掉，无任何效果 |

教训：`<font>` 标签即使颜色名不被支持也会被静默吃掉，所以**不能靠「标签没露出来」判断生效**，必须看有没有视觉变化。

---

## 9. Runner

### 9.1 调度（`dispatcher.py`）

- 全局一个 `asyncio.Queue(maxsize=max_queue)` + 一个常驻 worker。
- `TurnHandle`：`client`、`started_at`、`last_event_at`、`card_message_id`、`abort_reason`。
- 入队时：满 → 回「队列已满（N 条）」；前面有 N 条 → 回「已排队，前面还有 N 条」。
- `/stop`：清空队列 + `await handle.client.interrupt()`。
- 持有 `SessionConnection`，并起一个空闲监视任务：运行中不动它，空闲超过 `connection_idle_seconds` 就断开。退出时关闭连接。

### 9.2 连接复用（`connection.py`）

`SessionConnection` 同一时刻最多持有一个连接（A 模式只有一条活跃会话）：

| 方法 | 行为 |
|------|------|
| `acquire(session_id, options)` | 已有且同一会话 → 直接复用并返回 `(client, False)`；否则关掉旧的、`connect()` 新的，返回 `(client, True)` |
| `is_warm_for(session_id)` | 连接是否活着且对应这条会话 |
| `close(reason)` | `disconnect()` 并清空，记日志 |
| `disconnect_if_idle()` | 空闲超时才断开；`idle_seconds` 配 0 表示永不自动断开 |
| `state_text` | 给 `/status` 用的一行描述 |

**必须丢弃连接的时机**（漏掉任何一个都会让后续轮次拿到 `API Error: 422` 或者用错会话）：

1. 本轮 `handle.abort_reason` 非空（`/stop` 或超时中断）—— 见 2.4 第 3 条
2. `ResultError` / `ProcessError` / `CLIConnectionError` / 未预期异常
3. `/use` 切换会话（`acquire` 自己也会处理，但命令里显式关闭能更早释放会话）
4. 空闲超时
5. 桥退出

### 9.3 单轮（`session_runner.py`）

```python
# 连接还活着就说明会话是活的，不必再查文件；冷启动时才检查
if not connection.is_warm_for(active.session_id):
    if get_session_info(active.session_id, directory=active.workdir) is None:
        await fail("会话已不存在，请 /sessions 挑一条再 /use")   # 绝不新建
        return

options = ClaudeAgentOptions(
    cwd=active.workdir,
    resume=active.session_id,
    fork_session=False,
    permission_mode=cfg.runner.permission_mode,
    disallowed_tools=cfg.runner.disallowed_tools,
    setting_sources=cfg.runner.setting_sources,
    system_prompt={"type": "preset", "preset": "claude_code",
                   "append": cfg.runner.system_prompt_append},
    max_turns=cfg.runner.max_turns,
    model=cfg.runner.model,
    env=cfg.runner.env,
    cli_path=cfg.runner.cli_path,
    stderr=log_stderr,            # 只写日志，不上卡片
)

outcome = await docs.fetch(msg.text, active.short_id)    # 见 9.4
prompt = build_prompt(msg.text, outcome)

client, created = await connection.acquire(active.session_id, options)
handle.client = client
await client.query(prompt)

# 不能用 receive_response()：它遇到第一个 ResultMessage 就收尾，长连接下
# 那可能是上一轮后台任务的结果。必须自己按 origin 做关联，见 2.5
stream = TurnStream()
async for m in stream.iterate(client.receive_messages()):   # 不要 break
    handle.last_event_at = time.monotonic()
    renderer.feed(m)

# 关键：这里不要 disconnect。只有 9.2 列出的那五种时机才丢弃连接。
if handle.abort_reason:
    await connection.close("本轮被中断，连接不可复用")
```

`options` 另外要带 `add_dirs=[cfg.docs.inbox_dir]`，否则 Claude 读不到抓下来的云文档。

必须实现：

- **超时监控**：每 5 秒检查；超过 `turn_timeout_seconds`，或 `last_event_at` 距今超过 `idle_output_timeout_seconds` → `interrupt()`，卡片标「超时已中断」。
- **中断后继续读完** `receive_response()`（会收到 `ResultMessage`，`terminal_reason` 为 `aborted_*`）。
- **session_id 核对**：`ResultMessage.session_id` 与状态不一致时更新状态并记 WARN（实测正常情况下一致）。
- 成功后更新 `last_active_at`。
- **异常映射**（`except` 顺序 `ResultError` → `ProcessError` → `CLINotFoundError` → `CLIConnectionError` → `Exception`）：

| 情况 | 飞书提示 |
|------|----------|
| 会话不存在 | 「会话已不存在，请 /sessions 后 /use」，不重试不新建 |
| `CLINotFoundError` | 「找不到 Claude Code，请检查 runner.cli_path」 |
| `ResultError` / `is_error` | 显示 `subtype` + `errors` + 「可直接发消息继续」 |
| `ProcessError` | 「Claude 进程异常退出（exit N），下条消息会自动续上会话」 |
| 其他 | 「内部错误」+ 日志记全栈 |

### 9.4 云文档链接（`docs_fetch.py`）

场景：用户在对话里贴一个飞书云文档链接，Claude 要能读到内容。

为什么落地成文件而不是塞进 prompt：文档可能几万字，塞进 prompt 会挤掉会话上下文。写成文件后由 Claude 自己决定读多少。

```
消息文本 → 正则提链接（去重、上限 max_docs_per_message）
         → wiki: get_node(token) → obj_type/obj_token；只有指向 docx 的继续
         → docx: docx_blocks(token) 拿 block 树
                 ├─ 标题取 page 块
                 ├─ image_tokens() 按文档顺序取图片 token → media.download → <文档>.assets/img-NN.png
                 └─ docx_render.render(blocks, image_ref) → markdown
           blocks 接口失败时退回 raw_content（正文还在，图片位置会丢）
         → 写 inbox_dir/<会话ID前8位>/<标题>-<token前8>.md
         → prompt = 用户原文 + 「以下文档已抓到本机，请先读：<绝对路径>」
         → ClaudeAgentOptions.add_dirs = [inbox_dir]
```

| 链接类型 | 处理 |
|----------|------|
| `/docx/<token>` | block 树渲染，失败退回 `araw_content` |
| `/wiki/<token>` | 原生请求解析节点；指向 docx 的继续，其余按不支持处理 |
| `/sheets/` `/base/` `/docs/` `/file/` | 明确回「暂不支持<类型>，可以导出成文件，或改贴新版文档链接」 |
| 非飞书域名 | 不匹配，原样留在文本里 |

图片处理（`docx_render.py` + `docs_fetch.py`）：

- 图片写到 md 同目录的 `<文档名>-<token8>.assets/img-NN.<ext>`，扩展名按**文件头魔术字节**判断，不信任接口。
- markdown 里用 `![图 N](C:/绝对/路径.png)`（正斜杠），既是合法 markdown 也能直接喂给 Read 工具。文件头和 prompt 里都说明「需要看图时用 Read 读这个文件」。
- 遇到 `99991672`（应用缺 scope）或 HTTP 403（文档不允许下载素材）立刻停止本篇剩余图片，并给出对应的、可操作的改法。两种原因的提示必须不同，见 2.9。
- 其余零散失败折叠成一条提示，最多列 5 张图再给总数。
- 下载不到的图片在正文原位留「（图 N 未能下载，原因见本文件开头的说明）」，**不留空、不静默跳过**。

必须满足：

- 抓取失败时**绝不伪造内容**。失败原因进 prompt（让 Claude 在回复里提醒用户）并在卡片上留一行摘要。
- 权限失败的提示必须可操作，而且要区分两层：文档本身没授权（加协作者）vs 应用缺 scope（加权限并发布版本）。
- 文件名要过滤 Windows 非法字符；超长内容截断并在文件头标注。
- 抓取环节的任何异常都不能让这一轮失败，按「无文档」继续。
- 需要的飞书权限：`docx:document:readonly`、`wiki:wiki:readonly`、`drive:drive:readonly`（图片）。

### 9.5 消息里的图片和文件（`attachments.py`）

**为什么不是「收到附件就跑一轮」**：飞书的图片和文件都是独立消息，**不能带文字**。人的习惯是先把东西发过去、再打字说要干什么。收到就跑一轮的话，那一轮没有任何指令纯属浪费，而紧接着的文字消息又看不到附件。

所以：纯附件消息只**下载 + 回执**，不进队列；附件攒在 `PendingAttachments` 里，**下一条文字消息入队时领走**，拼进 prompt。

```
image 消息      → 下载 → 存入 pending → 回执「收到 N 张，把需求发给我」→ 不跑一轮
file 消息       → 同上，回执里列出原始文件名
post 消息       → 图文混排，一条消息里既有文字又有附件 → 直接跑一轮
text 消息       → 入队时 pending.take()，附件路径拼在需求后面
/stop /use /new → 清空 pending（换了上下文，旧附件不该再跟着跑）
其它类型        → 语音 / 视频等明确回「暂不支持这种消息（收到 xxx）」
```

图片和文件走同一条通道，只在两处分叉：下载时传给飞书的 `type`（`image` / `file`），以及落盘命名。

关键设计点：

- **在入队时领走，不是在执行时领走**。排队期间又发了新附件，不该混进前面那条需求。
- 容量检查必须发生在 `pending.take()` **之前**：否则队列满时附件已经从 pending 里清空、只留在被丢弃的局部变量里，文件还在磁盘上但再也不会被任何消息引用。
- `PendingAttachments` 有 TTL（默认 600 秒）和条数上限（默认 9 个）。超时的作废，超量的丢最旧的。
- 存 `attachments.dir/<会话ID前8位>/`，通过 `add_dirs` 授权 Claude 读取，和云文档的 inbox 是两个独立目录、两个独立开关。
- 落盘名统一带 `时间戳-消息ID片段-序号` 前缀。**只用时间戳会撞名**：同一秒内的两条消息（两连发，或两个人各自发）后写的会直接覆盖先写的。
- **图片**按文件头魔术字节判定扩展名，不信任消息里声明的类型；**文件**保留原始文件名（清洗掉 `<>:"/\|?*` 和控制字符、长度截断），好让 Claude 一眼看出是什么。
- prompt 里图片和文件**分两段列**：图片写「请用 Read 工具查看」，文件写「按扩展名判断类型，文本类直接 Read，二进制的用合适的命令处理」。按扩展名分类，所以文件消息发来的 `.png` 也算图片——Read 一样能看。
- **不按扩展名过滤文件类型**。在桥这一层猜「哪些类型有用」只会挡掉正当需求，`max_bytes` 才是闸门。代价是扔个 `.zip` 过去，Claude 可能只能告诉你它解不开。
- 需要权限 `im:resource`（图片和文件共用）。缺权限时**只给一条提示**并停止剩余附件（和云文档图片一样的处理，别刷 N 条）。
- **图片前提是模型支持视觉**，否则下载了也没用；文件没有这个限制。这一点配置里管不了，只能在 README 里写明。
- 群里发附件不会触发：附件消息带不了 @，`require_mention_in_group: true` 时会被忽略。

二进制下载接口的老问题在这里也一样：`message_resource.aget` 成功时 `resp.code` 是 None、`success()` 返回 False，必须看 `raw.status_code`。这段逻辑和 `drive media` 共用 `_binary_payload()`。

### 9.6 渲染（`render.py`）

| SDK 消息 | 渲染 |
|----------|------|
| `SystemMessage` | 不显示，记日志 |
| `AssistantMessage` / `TextBlock` | 追加正文 |
| `AssistantMessage` / `ToolUseBlock` | 追加一行 `**[工具] Bash**: npm test`（参数截断 80 字符）。用加粗而不是反引号，飞书不渲染行内代码 |
| `ThinkingBlock` | 不显示 |
| `ResultMessage` | 生成完成 / 失败卡片 |

- **节流**：脏标记 + 定时刷新，两次 `apatch` 间隔 ≥ `update_interval_ms`；最后一次必须发出。
- **串行化 patch**：刷新和定稿都发 `apatch`，必须共用一把锁。否则一次慢的「处理中」请求可能后到，把已经发出的最终卡片覆盖回去，看起来就像永远卡在运行中。定稿之后到达的节流刷新一律丢弃。
- **截断**：流式正文超 `max_card_chars` 保留尾部，开头加「…（前文已截断）」。

**长回答拆成多张卡片**（`split_for_cards()`）：

最终回答不超过 `result_inline_max` 就一张卡片发完；超过就按同样尺寸拆成多张——运行中那张**就地定稿成第 1 张**，其余作为新卡片回复到同一条用户消息下。

```
标题     完成 · <仓库>（2/3）
非末张   脚注写「回答较长，分 3 张卡片发出 · 接着看第 3 张」
末张     带耗时 / 轮数 / tokens —— 看到统计信息就说明读完了
超上限   最多 max_result_cards 张，多出来的截掉并在最后一张注明省略了多少字
```

- **按行切，不把一行劈开**（单行本身超长时才硬切，通常是压在一行里的长 JSON / base64）。
- **代码围栏跨卡片时，下一张开头补回 ` ```lang `**，收尾由 `to_feishu_markdown()` 的「补上未闭合围栏」那一步负责。不补开头的话，后半段代码会被当成正文渲染。
- 曾经的做法是**卡片放流式正文 + 纯文本补发全文**，两条消息、同一个答案，看起来像回了两遍，而且两条的内容还不是一回事（一个是整轮流式、一个是最终回答）。换成多卡片之后不再发纯文本。

**`to_feishu_markdown()`** —— 把标准 markdown 调成飞书能渲染的形式（依据 8.5 的实测结果）：

| 原文 | 处理 | 理由 |
|------|------|------|
| `## 修改说明` | → `**修改说明**` | 飞书不渲染标题，加粗是最接近的替代 |
| markdown 表格 | → 丢进 ```` ```text ```` 围栏 | 等宽字体能保住列对齐，比裸竖线可读 |
| 代码围栏 | 原样保留 | 飞书支持，还有行号和高亮 |
| 列表 | 原样保留 | `markdown` 组件支持嵌套 |
| 行内代码 `` `x` `` | → `<font color='blue'>x</font>` | 飞书不渲染行内代码，反引号会裸露出来。实测只有 `<font color>` 有效：`<code>` / `<u>` 被静默吃掉，`grey` 不在调色板里，`red` 像报错，加粗会和模型自己的加粗混在一起 |

两个边界情况：

- **顺序是先截断再转换**，因为转换的最后一步要补上未闭合的代码围栏。反过来的话截断可能把 ``` 切掉，导致后面全部内容被当成代码块。
- 围栏内部不做任何替换（里面的 `#` 是注释，不是标题）。
- `ResultMessage.result` 走的是另一条路（不经过累积正文），也必须单独过一遍这个转换。

---

## 10. 安全

1. 只有 `allowed_open_ids` 能驱动会话（留空 = 不限制，README 要警告）。
2. `permission_mode: bypassPermissions` 等于把本机 shell 交给白名单用户。`disallowed_tools` 是唯一兜底，README 必须写明风险。
3. `setting_sources` 含 `project` 时会加载目标仓库的 `.claude/`（hooks / MCP / skills）。**只在你信任的仓库的会话上用。**
4. 日志不得出现 `app_secret`；`config.yaml` 必须进 `.gitignore`。
5. 云文档抓取等于让白名单用户往本机磁盘写文件。inbox 固定在 `docs.inbox_dir`（仓库外），有单文档字数上限和单消息篇数上限。

---

## 11. 里程碑

| 阶段 | 产出 | 验收 |
|------|------|------|
| M0 骨架 | `pyproject.toml`、`config.py`、`__main__.py`、`.gitignore` | 配置错误时打印明确原因并退出 |
| M1 飞书回声 | `feishu/*`、`policy.py` | 群里 @ 发「hello」收到 `echo: hello`；未 @ 不回；非白名单回「无权限」；同 `message_id` 重推只回一次 |
| M2 会话切换 | `state.py`、`commands.py` | `/sessions` 列出本机会话（带仓库路径）；`/use 2` 成功并显示标题；非法序号/ID 明确报错；重启后活跃会话仍在 |
| M7 新建会话 | `/new`、`pending_create`、目录白名单 | `/new` 后发需求能跑通并落盘；未配白名单时 `/new <路径>` 被拒；白名单之外的目录被拒；新会话第二轮上下文延续 |
| M3 跑通一轮 | `session_runner.py`、`render.py`、卡片 | 发「在 README 加一行：飞书测试」，卡片流式更新→完成；终端 `claude --resume <id>` 能看到这轮对话 |
| M4 队列/中断/超时 | `dispatcher.py`、`/stop`、超时监控 | 运行中再发两条提示排队并顺序执行；`/stop` 后卡片显示「已中断」且下条消息能续上；`idle_output_timeout_seconds=10` 时让它 `sleep 60` 能自动中断 |
| M5 云文档链接 | `docs_fetch.py`、`docx_render.py`、`add_dirs` | 贴 docx 链接后 Claude 能读到正文和图片；贴表格链接明确回不支持；无权限时提示具体要加什么且不伪造内容 |
| M6 收尾 | README、冒烟脚本 | `scripts/smoke_runner.py` 全绿；`ruff check` 无报错；按第 12 章走一遍 |

**实现状态（2026-11-12）**

M0–M7 已完成。之后又加了两项：消息里的**文件**附件（和图片共用 9.5 的通道）、长回答**拆成多张卡片**（9.6）。

**自动化验证**

- `scripts/smoke_runner.py`：198 项检查全绿（真实 Claude SDK + 假飞书 API）。覆盖 resume 上下文延续、会话丢失、命令解析、队列上限、超时中断、连接复用与丢弃、`/new` 真实建会话、后台任务串轮（回放实测序列）、云文档抓取与图片、消息里的图片和文件、长回答拆卡片、飞书 markdown 转换、各种降级路径。
- `scripts/check_feishu_doc.py`：真实凭据 + 真实文档（234 block）。正文 9268 字（纯文本接口只有 6883 字），24 张图位置全部正确。
- `scripts/check_card_markdown.py`：三张对照卡片确定了飞书 markdown 的支持范围（见 8.5）。
- `scripts/bench_startup.py`：连接复用的收益（见 2.3）。

**真机验证过（飞书私聊）**

- 长连接收发、事件接收、私聊免 @
- `/sessions` 列表、`/use` 序号与 ID 前缀
- 真实跑一轮：卡片流式更新 → 完成卡片（耗时 / 轮数 / token）
- 卡片 markdown：代码块带行号高亮、列表样式、表格对齐、行内代码蓝色
- 云文档链接：正文可读；图片因文档「仅可编辑的人可下载」而 403，降级提示符合预期
- 消息图片：纯图片回执不跑一轮 → 下一条需求带上图 → Claude 能看到图的内容
- 卡片节流：长任务中途确实逐步刷新（真实跑一轮时每 1~20 秒一次 `apatch`，见日志）

**仍未真机验证**

- 文件附件：下载、落盘名、Claude 实际读到文件内容（自动化验证已过）
- 长回答拆多张卡片：飞书里连续多张卡片的实际观感（自动化验证已过）
- 运行中连发多条的排队表现
- 空闲超时自动断连
- 重启后残留「处理中」卡片被刷成「状态未知」
- 长连接跑久后触发上下文自动压缩的行为
- 群聊里的 @ 校验与非白名单拒绝

---

## 12. 端到端验收清单

- [ ] `/sessions` 列出的仓库路径与 `SDKSessionInfo.cwd` 一致
- [ ] `/use` 后发一句话，**同一 session_id** 继续，上下文还在（终端 resume 可见）
- [ ] 非白名单用户发言被拒
- [ ] 运行中第二条消息排队，不打乱会话
- [ ] `/stop` 能停，之后能继续
- [ ] 会话文件被删后发消息明确报错，**没有**偷开新会话
- [ ] 重启桥后活跃会话还在；上一张「处理中」卡片被刷成「状态未知」
- [ ] 卡片上没有费用数字（2.6）
- [ ] 让它起一个后台任务（比如「后台轮询某个 URL，起来了告诉我」），下一条消息问**别的事**，卡片回的必须是新问题的答案，不是后台任务的输出（2.5）
- [ ] stderr 噪音没有被当成错误贴到卡片上
- [ ] 回复里的代码块在卡片上渲染成灰底框（不是裸的 ```），列表有样式，标题变成加粗
- [ ] 贴 docx 链接后 Claude 确实读到了内容（回复里引用了文档里的具体内容）
- [ ] 文档里的图片能被 Claude 看到（让它描述某张图的内容来验证）
- [ ] 机器人没有文档权限时，明确提示加协作者，**没有**编造文档内容
- [ ] 应用缺 `drive:drive:readonly` 时，正文照常抓到、图片位置留占位说明、提示里给出具体权限名
- [ ] 贴表格 / 多维表格链接时明确回「暂不支持」并给替代方案
- [ ] `/new` 后发需求，终端 `claude --resume <新id>` 能看到这条新会话
- [ ] 未配 `allowed_workdir_roots` 时 `/new <路径>` 被拒并说明怎么配
- [ ] 发一张截图 → 收到回执且不跑一轮；接着发文字需求 → 卡片标注「带了 N 张图片」且 Claude 的回答确实提到了图里的内容
- [ ] 发一个文件（如 `.html`）→ 回执里列出原始文件名；接着发需求 → 卡片标注「带了 N 个文件」且 Claude 确实读到了文件内容
- [ ] 落盘文件名保留了原始文件名，带非法字符的文件名被清洗且没有逃出 `attachments.dir`
- [ ] `/stop` 之后攒着的附件被清掉
- [ ] 让它写一段长回答（超过 `result_inline_max`）→ 拆成多张卡片，标题带 `（n/N）`，统计信息只在最后一张，**没有**额外的纯文本全文
- [ ] 长回答里包含代码块且正好跨卡片 → 后半段仍然渲染成代码块，不是正文

---

## 13. v2 待办（第一版明确不做）

- ask 模式：`permission_mode="default"` + `can_use_tool` + 飞书审批卡片 + `card.action.trigger` 回调。SDK 侧 `can_use_tool`、`PermissionResultAllow/Deny`、`hooks` 均已确认存在，卡片回调的 `CallBackAction/Operator/Toast` 也已确认，可直接接。
- B 模式：按 `chat_id` 多绑定并行。
- `AskUserQuestion` 做成飞书选项卡片（第一版由 `system_prompt_append` 要求模型自行假设）。
- `include_partial_messages=True` 做 token 级流式（第一版按消息块更新够用）。
- **上下文水位与溢出处理**（2.10 挖出来的三个缺口）：识别上下文溢出并给出明确提示 + ERROR 日志、`/status` 显示 `get_context_usage()` 的水位、接近上限时卡片提醒。可能还要把 `fork_session` 包成一个命令（`/fork <水位>`）。
- **更多云文档类型**：电子表格（`sheets.v3`）、多维表格（`bitable.v1`）、旧版文档（`drive.v1.export_task` 异步导出）。
- Watcher / PR Reviewer / 项目知识文件。

---

## 14. 参考

- Agent SDK 概览：<https://code.claude.com/docs/en/agent-sdk/overview>
- Python SDK 参考：<https://code.claude.com/docs/en/agent-sdk/python>
- 会话（resume / list / 存储）：<https://code.claude.com/docs/en/agent-sdk/sessions>
- 权限模式：<https://code.claude.com/docs/en/agent-sdk/permissions>
- 飞书 Python SDK：<https://github.com/larksuite/oapi-sdk-python>
