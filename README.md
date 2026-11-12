# on_call_bot — 飞书 ↔ Claude Code 会话桥

把本机**已存在**的一条 Claude Code 会话接到飞书机器人上。在飞书里发一句话，这条会话就在它自己的仓库里继续干活，过程流式回到飞书卡片。

任意时刻只驱动**一条**会话（全局单活跃）。会话自带固定工作目录，用 `/sessions` 看列表、`/use` 切换。

设计与实测结论见 [`docs/飞书绑定Claude-Code会话-设计试用指南.md`](docs/飞书绑定Claude-Code会话-设计试用指南.md)。

---

## 它不做什么

- **不自动新建会话。** 会话文件没了就明确报错，让你去终端开一条再 `/use`。
- 不做多群并行。谁发消息都驱动同一条会话，输出回到发消息的那个窗口。
- 不做权限审批卡片。第一版只有 `bypassPermissions`，靠工具黑名单兜底（见下面的风险）。
- 不支持语音 / 视频消息。图片、文件附件和云文档**链接**都支持（见后文）。
- 必须跑在会话所在的那台机器上，关机即不可用。

---

## 1. 环境

```powershell
conda create -n oncall python=3.12 -y
conda activate oncall
pip install -e .
```

本机需要已安装并登录过 Claude Code（`claude --version` 能跑通）。模型与认证**自动继承** `~/.claude/settings.json`，不需要在本项目里再配一遍。

验证环境（不需要飞书凭据，会真实调用一次模型）：

```powershell
python scripts/smoke_runner.py
```

正常输出以 `全部通过` 结束，覆盖 resume 上下文延续、会话丢失处理、命令、队列、超时中断、云文档抓取、图片与文件附件。

---

## 2. 飞书开放平台配置

1. 创建**企业自建应用**，开启「机器人」能力。
2. 权限（最小集）：

   | 权限 | 用途 |
   |------|------|
   | `im:message` | 读消息 |
   | `im:message:send_as_bot` | 发消息 / 发卡片 |
   | `im:message.p2p_msg:readonly` | **私聊消息（主要用法，必须）** |
   | `im:message.group_at_msg:readonly` | 群里被 @ 的消息（只在群里用才需要）|
   | `im:resource` | **下载消息里的图片和文件**（开启 `attachments.enabled` 才需要）|
   | `docx:document:readonly` | 读新版云文档正文（开启 `docs.enabled` 才需要）|
   | `wiki:wiki:readonly` | 解析知识库链接（同上）|
   | `drive:drive:readonly` | **下载文档里的图片**（没有这个就只有文字，图片位置会留占位说明）|

   权限改完必须**发布新版本并等审核通过**，否则接口仍然返回 `99991672`。

3. 事件订阅：选「**使用长连接接收事件**」，订阅 `im.message.receive_v1`。
4. 回调配置：选「**使用长连接接收回调**」，订阅 `card.action.trigger`。第一版的卡片没有按钮，这一步可以先跳过，留给以后的审批卡片。
5. 「版本管理与发布」里把**可用范围**设成包含你自己（私聊机器人要求应用对你可见），然后发布版本等审核通过。

长连接模式不需要公网地址，也不需要 `verification_token` / `encrypt_key`。

**私聊用法不需要建群、不需要 @ 机器人。** 审核通过后在飞书里搜应用名就能直接私聊。

拿到你自己的 `open_id`：先把 `allowed_open_ids` 留空启动一次，私聊机器人发一句 `/help`，日志里会有

```
收到消息 chat_type=p2p sender_open_id=ou_xxxxxxxxxxxx 长度=5
```

把那个 `ou_` 开头的值填进 `allowed_open_ids`，重启。**留空等于谁都能驱动你的本机 shell，别忘了填回去。**

---

## 3. 配置

```powershell
Copy-Item config.example.yaml config.yaml
```

**必填只有三项**：`feishu.app_id`、`feishu.app_secret`、`policy.allowed_open_ids`（不填等于谁都能驱动你本机的 shell）。其余全有默认值，不写就用默认。

`config.yaml` 已在 `.gitignore` 中，不要提交。配置不合法时启动会打印具体哪个字段错并**退出码 2**；长连接不可恢复地断掉是**退出码 1**（留给外部守护进程重启）。

下面是全部字段。默认值和取值范围取自 `src/oncall_bot/config.py`，`config.example.yaml` 的键与它逐项核对过。

### `feishu`

| 字段 | 默认 | 说明 |
|------|------|------|
| `app_id` | **必填** | 开放平台应用 ID。仍是 `cli_xxx` 这类占位符会被拒 |
| `app_secret` | **必填** | 应用密钥。永不进日志 |

### `policy`

| 字段 | 默认 | 说明 |
|------|------|------|
| `require_mention_in_group` | `true` | 群聊里必须 @ 机器人才响应。私聊不受影响 |
| `allowed_open_ids` | `[]` | 能驱动会话的人。**留空 = 不限制**，仅私聊自用时可接受。第一次配置时先留空启动，发一条消息，从日志的 `sender_open_id=` 里抄自己的 ID |
| `allowed_workdir_roots` | `[]` | `/new <路径>` 只允许这些根目录之下的目录。**留空 = `/new` 不接受路径参数**，只能沿用当前会话的仓库。这是安全默认：放开等于让飞书上的人在本机任意目录执行工具。路径会自动展开 `~` 并取绝对路径 |

### `runner`

| 字段 | 默认 | 说明 |
|------|------|------|
| `cli_path` | `null` | `null` = 用 claude-agent-sdk 自带的 CLI；也可填本机 `claude` 可执行文件路径 |
| `permission_mode` | `"bypassPermissions"` | 可选 `default` / `acceptEdits` / `plan` / `bypassPermissions` / `dontAsk` / `auto`。默认值等于工具直接执行、不询问，**风险见第 6 节** |
| `disallowed_tools` | `rm -rf*` / `git push --force*` / `git reset --hard*` / `git clean -fd*` 四条 | `bypassPermissions` 下仍然生效，是唯一兜底 |
| `setting_sources` | `["user", "project"]` | 加载哪些配置来源，可选 `user` / `project` / `local`。含 `project` 时会加载目标仓库的 `.claude/`（hooks / MCP / skills） |
| `model` | `null` | `null` = 继承本机 `~/.claude/settings.json` 里的模型配置 |
| `max_turns` | `60` | 单轮内最多几个助手回合，`> 0` |
| `turn_timeout_seconds` | `1800` | 单轮总时长上限，超了等同 `/stop`，`> 0` |
| `idle_output_timeout_seconds` | `300` | 连续多久没有任何输出就中断，`> 0` |
| `max_queue` | `5` | 排队上限，满了明确拒绝新消息，`> 0` |
| `connection_idle_seconds` | `900` | Claude 连接跨轮复用，空闲这么久后断开。**`0` = 永不自动断开**，`>= 0`。见[连接复用](#连接复用) |
| `env` | `{}` | 透传给 CLI 的环境变量，只在需要覆盖网关 / 模型时才填（如 `ANTHROPIC_BASE_URL`） |
| `system_prompt_append` | `""` | 追加在 `claude_code` 预设 system prompt 后面。用来告诉模型「你在飞书里被远程调用，用户看不到终端、无法回答交互式提问」 |

### `render`

| 字段 | 默认 | 说明 |
|------|------|------|
| `update_interval_ms` | `1200` | 卡片刷新的节流间隔，`>= 200` |
| `max_card_chars` | `6000` | 单张卡片正文字数上限，`>= 500`。飞书卡片本身有体积限制，别调太大 |
| `result_inline_max` | `5500` | 最终回答一张卡片装多少字，`>= 500`。超过就按这个尺寸拆成多张卡片。填得比 `max_card_chars` 大会被收敛成后者并记一条 warning |
| `max_result_cards` | `6` | 回答再长也最多拆这么多张，多出来的截掉并在最后一张说明。`1 ~ 20` |
| `session_list_limit` | `10` | `/sessions` 列几条，`1 ~ 50` |

### `docs`（飞书云文档链接 → 本地 md）

| 字段 | 默认 | 说明 |
|------|------|------|
| `enabled` | `true` | 关掉后消息里的云文档链接原样交给 Claude，不抓取 |
| `inbox_dir` | `~/.oncall-bot/inbox` | 抓下来的文档落在这里，通过 `add_dirs` 授权给 Claude；**不写进目标仓库** |
| `max_chars_per_doc` | `200000` | 单篇正文字数上限，`> 0` |
| `max_docs_per_message` | `5` | 一条消息最多抓几篇，`1 ~ 20` |
| `fetch_images` | `true` | 是否下载文档内嵌图片到 `<文档>.assets/` |
| `max_images_per_doc` | `40` | 单篇最多下载几张图，`0 ~ 200` |
| `max_image_bytes` | `10485760`（10 MB） | 单张图片大小上限，`> 0` |

### `attachments`（消息里的图片和文件）

| 字段 | 默认 | 说明 |
|------|------|------|
| `enabled` | `true` | 关掉后收到图片 / 文件只回一句「附件处理已关闭」 |
| `dir` | `~/.oncall-bot/attachments` | 落盘目录，按 `<会话ID前8位>/` 分子目录，通过 `add_dirs` 授权给 Claude |
| `max_bytes` | `20971520`（20 MB） | 单个附件大小上限，`> 0`。**不按扩展名过滤文件类型，这是唯一的闸门** |
| `max_pending` | `9` | 最多攒几个等下一条需求领走，`1 ~ 50`。超了丢最旧的 |
| `pending_ttl_seconds` | `600` | 攒着的附件多久作废，`> 0` |

### 顶层

| 字段 | 默认 | 说明 |
|------|------|------|
| `state_file` | `~/.oncall-bot/state.json` | 活跃会话 + 最后一张卡片的引用。临时文件 + `os.replace` 原子写 |
| `log_dir` | `~/.oncall-bot/logs` | 日志目录，`oncall-bot.log` 按 8 MB 轮转留 5 份 |

---

## 4. 启动

```powershell
conda activate oncall
python -m oncall_bot --config config.yaml
```

加 `--debug` 开 DEBUG 日志。日志同时写到控制台和 `~/.oncall-bot/logs/oncall-bot.log`（8MB 轮转，留 5 份）。

Windows 开机自启：用「任务计划程序」新建任务，触发器选「用户登录时」，操作填

- 程序：`C:\Users\<你>\.conda\envs\oncall\python.exe`
- 参数：`-m oncall_bot --config config.yaml`
- 起始于：`d:\Workplace\on_call_bot`

---

## 5. 用法

**私聊**：直接发消息，不用 @。
**群聊**：需要 @ 机器人（`policy.require_mention_in_group: true` 时）。

> 私聊没有 @ 这道闸门，意味着**你发的每一句话都会被转给 Claude**，包括「嗯」「好」这种。想随口说话又不触发，就先发 `/stop`，或者换个思路：把闲聊放在别处。命令（`/` 开头）不会转给 Claude。

### 发图片和文件

直接在飞书里把图片或文件发给机器人。**附件不会单独触发一轮**——飞书的图片 / 文件都是独立消息、不能带文字，所以机器人只下载 + 回执：

```
你：  [一张报错截图]
机器人：收到 1 张图片（共 84 KB），已存到本机。把需求发给我，我会带着这些附件一起看。
你：  按这个报错改一下
机器人：[处理中，带了 1 张图片 …]
```

```
你：  [dreame-rd-digital-platform.html]
机器人：收到 1 个文件（共 960 KB），已存到本机。
       - dreame-rd-digital-platform.html
       把需求发给我，我会带着这些附件一起看。
你：  看下这个 demo 怎么样，有什么设计上的亮点和弊端
机器人：[处理中，带了 1 个文件 …]
```

附件存在 `~/.oncall-bot/attachments/<会话ID前8位>/`，通过 `add_dirs` 授权给 Claude。文件保留原始文件名（前面加时间戳和消息 ID 片段防撞名），图片按文件头判定扩展名。prompt 里图片和文件分开列：图片让 Claude 用 Read 看，文件由它按扩展名自己决定怎么读。

**不按类型过滤文件。** 在桥这一层猜「哪些类型有用」只会挡掉正当需求，大小上限（`attachments.max_bytes`，默认 20 MB）才是闸门。代价是扔个 `.zip` 过去，Claude 可能只能告诉你它解不开。

连发几个再说需求也可以，默认最多攒 9 个、10 分钟内有效（`attachments.max_pending` / `pending_ttl_seconds`）。

富文本消息（图文混排）是一条消息里既有文字又有附件，这种直接跑一轮，不用分两步。

`/stop`、`/use`、`/new` 都会清掉攒着的附件——换了上下文，之前的附件就不该再跟着跑。`/status` 能看到当前攒了几个。

需要权限 `im:resource`。图片**还需要你的模型支持视觉**，否则图下载了 Claude 也看不见；文件没有这个限制。

### 新建会话

来了个新需求、不想接着旧上下文往下说时用 `/new`：

```
/new                      在当前仓库下新建，上下文从零开始
/new D:/Workplace/tmd-pbi  在别的仓库下新建
```

新建之后直接把需求发过去就行。**会话文件要跑完第一轮才落盘**，在那之前这条会话不会出现在 `/sessions` 列表里，`/status` 会标「尚未落盘」。

`/new <路径>` 默认是**禁用**的，必须先配 `policy.allowed_workdir_roots`：

```yaml
policy:
  allowed_workdir_roots:
    - "D:/Workplace"
```

不配的话 `/new <路径>` 会被拒绝并告诉你怎么配。理由是 `/new <任意路径>` 配上 `bypassPermissions`，等于让飞书上的人在本机任何目录启动 Claude 并直接执行工具。不带参数的 `/new` 不受这个限制，因为它只沿用当前会话已有的仓库。

> 「绝不自动新建会话」这条约束仍然成立：只有你打 `/new` 才会新建。会话丢失时机器人只会报错让你重选，不会偷偷开一条。

### 连接复用

Claude 连接**跨轮复用**，不是每条消息都重启一次。实测在 19MB 的会话上，每轮新建连接要 11.75 秒，复用之后稳定在 1.62 秒。

连接会在这些情况下被丢弃，下一条消息重连（多付一次约 8 秒）：

- `/stop` 或超时中断 —— 实测 `interrupt()` 之后连接不可复用，必须重连
- `/use` 切换会话
- Claude 进程异常退出
- 空闲超过 `runner.connection_idle_seconds`（默认 900 秒，设成 0 则永不自动断开）

`/status` 能看到当前连接状态。

**代价**：桥在连接期间一直占着这条会话。本来就不该在终端里打开同一条会话，现在更不行。想去终端用，先 `/use` 切到别的会话，或者等空闲超时断开。

| 命令 | 说明 |
|------|------|
| `/sessions` | 列出本机最近的 Claude Code 会话（序号、ID 前 8 位、标题、仓库路径、分支、最后活动） |
| `/use <序号\|会话ID>` | 切换活跃会话。序号来自最近一次 `/sessions`；会话 ID 可以直接用列表里显示的前 8 位，只要唯一匹配。匹配到多条会列出候选让你再选 |
| `/new` | 在**当前会话的同一个仓库**下新建一条会话。新需求用这个，上下文从零开始 |
| `/new <路径>` | 在指定仓库下新建。需要先配 `policy.allowed_workdir_roots` |
| `/status` | 当前会话、运行状态、队列长度、Claude 连接状态、权限模式 |
| `/stop` | 中断当前任务并清空队列 |
| `/help` | 命令说明 |

直接发文字就是把需求交给当前会话。运行中再发会排队（上限 `runner.max_queue`），严格串行。
排队等待的时间不计入 `runner.turn_timeout_seconds`，超时是从真正开始跑算起的。
队列满时会明确拒绝这一条，但**不丢已攒的附件**，下一条成功的需求仍会带上。

**切换会话前先 `/stop`。** 运行中 `/use` 会被拒绝。

**桥使用某条会话期间，不要在终端里同时打开同一条会话** —— 两边同时写会弄乱历史。

### 云文档链接

直接在消息里贴飞书云文档链接即可：

```
@机器人 按这个文档改一下 https://xxx.feishu.cn/docx/AbCd1234...
```

桥会先把文档抓成本地 md（放在 `~/.oncall-bot/inbox/<会话ID前8位>/`），再把路径附在需求后面交给 Claude。inbox 不在你的仓库里，通过 `add_dirs` 单独授权给 Claude 读取，不会污染代码库。

抓到的内容包括：

- **标题层级、有序/无序列表、代码块、引用、待办、分割线** —— 正文走 block 树渲染，不是纯文本接口
- **文档里引用的其他文档** —— 渲染成 `[标题](真实链接)`，Claude 想继续追就能追
- **内嵌图片** —— 下载到 md 同目录的 `.assets/` 文件夹，正文里用本机绝对路径引用，Claude 用 Read 工具就能看图

> 为什么不用飞书的纯文本接口：它会丢图片位置。实测一篇 234 个 block 的文档有 24 张图，纯文本接口只给出 23 个 `image.png` 占位符（有一张被包在容器块里直接跳过了），按顺序往回填必然错位。走 block 树则位置完全准确，正文也从 6883 字变成 9268 字。

支持：新版文档（`/docx/`）、知识库里指向新版文档的节点（`/wiki/`）。
不支持：电子表格、多维表格、旧版文档、云空间文件 —— 会明确告诉你不支持，并建议导出成文件。

**权限是最常见的坑**，分三层，机器人会告诉你是哪一层，不会假装读到了：

1. **文档本身的访问权**：机器人是独立身份，把文档分享到群里它**读不到**。需要在文档的「分享」里把机器人加为协作者（可阅读），或者把文档设为组织内可阅读。
2. **应用的接口权限**：图片下载要 `drive:drive:readonly`（或 `docs:document.media:download`）。缺这个时飞书返回 `99991672`，提示里会带上要申请的 scope 名。
3. **文档的下载限制**：第 1、2 层都满足了，图片仍可能 403。文档「更多设置」里「可复制、打印、下载」默认常限制为**仅可编辑的人**，而机器人只有可阅读。改法二选一：把机器人的协作者权限提升到「可编辑」，或把该设置放宽到「可阅读的人」。

> 第 3 层容易误判成第 2 层。区别是：缺 scope 返回 HTTP 400 + `99991672`，并列出可申请的权限清单；文档不让下载返回 **HTTP 403 且响应体完全为空**。实测一篇带保密水印的文档就是这种，正文 9268 字全部读到，24 张图和 2 个附件全部 403。

任何一层没过，正文都照常抓取，图片位置在原处留占位说明。

---

## 6. 风险，使用前必读

1. **`permission_mode: bypassPermissions` 等于把本机 shell 交给白名单里的人。** Claude 会直接执行工具调用，不询问。`runner.disallowed_tools` 是唯一兜底，默认拦了 `rm -rf`、`git push --force`、`git reset --hard`、`git clean -fd`。按你的实际情况加。
2. **`setting_sources` 含 `project` 时会加载目标仓库的 `.claude/`**（hooks、MCP、skills）。只在你信任的仓库的会话上用。不想要就把它改成 `["user"]`。
3. `policy.allowed_open_ids` 留空表示**不限制任何人**。生产使用务必填上。
4. 云文档会落到本机磁盘。开启 `docs.enabled` 前确认 inbox 目录位置可接受。

---

## 7. 项目结构

```
src/oncall_bot/
├─ __main__.py        入口：加载配置 → 主循环 → 飞书长连接线程
├─ config.py          pydantic 配置模型与校验（字段表见第 3 节）
├─ logging_setup.py   文件轮转 + 控制台日志
├─ app.py             消息主流程（去重 → @ 校验 → 白名单 → 命令 → 入队）
├─ policy.py          message_id TTL 去重
├─ state.py           全局活跃会话 + 原子持久化
├─ commands.py        /sessions /use /new /status /stop /help
├─ attachments.py     消息里的图片 / 文件 → 下载、攒着、拼进 prompt
├─ docs_fetch.py      飞书云文档链接 → 本地 md（含图片下载）
├─ docx_render.py     文档 block 树 → markdown
├─ feishu/
│  ├─ ws.py           长连接线程（独立 event loop）
│  ├─ api.py          areply / apatch / 机器人 open_id / docx / wiki
│  ├─ cards.py        卡片 JSON（纯函数）
│  └─ parser.py       事件 → IncomingMessage
└─ runner/
   ├─ connection.py   跨轮复用的 Claude 连接
   ├─ turn_stream.py  区分本轮消息与后台任务消息
   ├─ dispatcher.py   单队列 + worker + /stop + 空闲断连
   ├─ session_runner.py  用 ClaudeSDKClient resume 跑一轮
   └─ render.py       SDK 消息 → 卡片正文 + 节流更新
```

---

## 8. 卡片上的 markdown

飞书卡片的 markdown 支持是个子集，实测结果（`scripts/check_card_markdown.py` 发对照卡片验的）：

**能直接渲染**：加粗、斜体、删除线、链接、代码围栏（带行号和语法高亮）、有序/无序列表及嵌套、`---` 分割线。

**不能渲染**，渲染层做了等价替换：

| 原文 | 卡片上显示成 | 为什么这么选 |
|------|-------------|-------------|
| `## 标题` | **标题**（加粗）| 飞书不渲染任何级别的标题 |
| markdown 表格 | 对齐的等宽文本块 | 竖线和 `\|---\|` 分隔行会丢掉，中日韩字符按双宽计算列宽 |
| 行内代码 `` `x` `` | <font color='blue'>蓝色文字</font> | 实测只有 `<font color>` 有效：`<code>` 和 `<u>` 标签被静默吃掉，`grey` 不在调色板里，`red` 像报错，`**粗体**` 会和模型自己的加粗混在一起 |

想自己复验（都发到 `allowed_open_ids` 里第一个人的私聊）：

```powershell
python scripts/check_card_markdown.py            # 语法对照卡片
python scripts/check_card_markdown.py --inline   # 行内强调写法对照卡片
python scripts/check_card_markdown.py --sample   # 走真实渲染管线的示例卡片
```

> 一个坑：`<font>` 标签即使颜色名不被支持，标签本身也会被静默吃掉。所以**不能靠「标签没露出来」判断生效**，必须看有没有视觉变化。

### 回答很长的时候

最终回答不超过 `render.result_inline_max`（默认 5500 字）就一张卡片发完。超过就**按同样的尺寸拆成多张卡片**：运行中那张就地定稿成第 1 张，后面几张作为新卡片回复到同一条消息下。

- 标题带张数：`完成 · Windchill（2/3）`，非末张的脚注写「接着看第 3 张」。
- 耗时 / 轮数 / tokens 只放在**最后一张**上 —— 看到统计信息就说明读完了。
- 代码围栏跨卡片时，下一张开头会补回 ```` ```lang ````，否则后半段代码会被当成正文渲染。
- 最多拆 `render.max_result_cards` 张（默认 6），再长就截掉尾部并在最后一张写明省略了多少字。完整内容始终在本机的会话历史里。

> 以前的做法是卡片 + 纯文本全文各发一条，同一个答案看起来回了两遍，现在换成多卡片。`result_inline_max` 填得比 `max_card_chars` 大会被收敛成后者并记一条 warning —— 只想调小单卡容量的人不用把两个值都改。

---

## 9. 已知限制

- 桥必须和会话在同一台机器上。
- 同一条会话不能同时在终端和飞书里用。
- 卡片上**不显示费用**：本机走的是第三方网关，SDK 报的 `total_cost_usd` 是按 Anthropic 价目表估的，和实际计费无关。显示的是耗时 / 轮数 / token 数。
- `AskUserQuestion` 这类交互式提问走不通，`system_prompt_append` 里已要求模型基于合理假设继续并列出假设。
- 桥在某轮中途被杀掉时，那张「处理中」卡片会在下次启动时被刷成「状态未知」，但那一轮的真实结果需要你去终端 `claude --resume <id>` 确认。
- **会话上下文会撑满。** 同一条会话被桥长期 resume 只会越来越大，撞到模型窗口上限时网关直接返回 `400 ... maximum context length`。实测自动压缩在这条路上没生效，到那个点上 `/compact` 本身也超窗口、跑不动。办法是 `/new` 换新会话，或用 `fork_session` 把老会话截断成一条能用的新会话 —— 过程和实测数据见[设计文档 2.10](docs/飞书绑定Claude-Code会话-设计试用指南.md)。另外这个报错目前会被当成模型的正常回答显示在卡片上，日志里查不到。
