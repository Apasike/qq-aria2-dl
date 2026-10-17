# QQ 推送自动下载机器人：设计思路与开发大纲

> 状态：设计稿 v0.1  
> 最后更新：2026-10-17  
> 项目目标：在 QQ 群/私聊中接收链接，自动提交到 aria2 下载，并在完成后向来源会话播报。

---

## 1. 一句话结论

本项目采用 OneBot v11 与 NapCat 通信，把消息接入、任务编排、进度查询、aria2 状态对账和通知收敛到一个单进程服务中，使用 SQLite 保存业务上下文。运行结构保持三个进程：

```text
NapCat
qq-aria2-dl（单进程 bot）
aria2c
```

FastAPI 作为 bot 进程内的 HTTP/WebSocket 框架使用，负责 WebSocket 接入、健康检查和后续管理接口。

---

## 2. 目标与非目标

### 2.1 目标

- 接收 QQ 群消息和私聊消息。
- 支持手动命令下载，例如 `/dl <url>`。
- 支持白名单群/用户的自动链接识别与下载。
- 对重复链接去重，避免同一链接被反复提交。
- 下载前检查磁盘空间，下载过程中持续保护磁盘。
- 使用 aria2 管理下载队列、重试、续传和并发。
- 下载完成/失败后，向任务来源群或私聊发送通知。
- bot 或 aria2 重启后，能够恢复任务状态并补发未发送通知。
- 具备基本的安全边界：QQ 白名单、限流、URL 校验、RPC token。
- 部署简单，优先支持单机 Docker Compose 或系统服务。

### 2.2 非目标（第一阶段不做）

- 多租户、多实例、水平扩展。
- 复杂的插件系统或多平台适配。
- Web 管理后台。
- 下载后自动解压、转码、上传网盘等重后处理。
- 把大文件重新上传到 QQ。
- 高并发下载平台级别的能力。
- OneBot v12 适配。

这些能力可以后续按需增加，但不应污染第一版的核心链路。

---

## 3. 目标运行形态

系统只保留三个长期运行的进程：

```text
NapCat
qq-aria2-dl（单进程 bot）
aria2c
```

目标链路：

```text
QQ 群/私聊
   │
NapCat（OneBot v11）
   │ Reverse WebSocket
   │ ws://bot:8080/onebot/v11/ws
   ▼
qq-aria2-dl（单进程 asyncio）
   ├─ OneBot transport：收事件 / 发 action
   ├─ Router + ACL：命令、白名单、限流
   ├─ TaskService：去重、空间检查、提交任务
   ├─ ProgressService：进度缓存、查询与格式化
   ├─ aria2 JSON-RPC client
   ├─ Reconcile loop：状态对账与通知
   └─ SQLite：任务上下文与通知状态
   │ HTTP JSON-RPC
   ▼
aria2c daemon
   │
/data/downloads
```

核心目标链路：

```text
收消息 → 鉴权 → 解析命令/链接 → 去重 → 空间检查
      → 提交 aria2 → 查询/展示进度 → 完成对账 → 通知
```

---

## 4. 设计原则

1. **aria2 是下载状态的唯一事实来源（source of truth）**  
   SQLite 只保存“这个 gid 是谁提交的、完成后通知哪个会话”等业务上下文，不重复维护完整的下载状态机。

2. **对账优先于一次性事件**  
   不假设下载完成事件一定送达 bot。通过周期性 `tellStatus` 对账，保证 bot 重启、NapCat 掉线后仍能补发通知。

3. **单进程、单事件循环、单 uvicorn worker**  
   避免 WS 连接、内存限流器、去重锁和进度缓存等状态被多个 worker 分裂。若未来需要多实例，再单独设计共享状态方案。

4. **适配器边界清晰**  
   OneBot、aria2、SQLite 分别封装为 adapter；命令处理器只依赖 TaskService，不直接拼 RPC。

5. **幂等**  
   - 同一条 QQ 消息只处理一次（`message_id`）。
   - 同一个链接在 TTL 内只提交一次（去重键）。
   - 同一个任务只通知一次（`notified_at`）。
   - 同一个 gid 不重复创建。

6. **默认拒绝**  
   QQ 群、私聊、URL scheme、文件名、磁盘空间不满足条件时，默认不下载，并给出明确原因。

7. **先解决问题，不提前抽象**  
   第一版不做通用任务队列、插件系统、事件总线。代码结构保持可测试即可。

---

## 5. 总体架构

### 5.1 组件职责

| 组件 | 职责 |
|---|---|
| NapCat | QQ 协议适配，暴露 OneBot v11 事件和 action |
| qq-aria2-dl | 收消息、鉴权、去重、任务编排、进度查询、状态对账、发送通知 |
| aria2c | 下载队列、并发控制、续传、重试、限速 |
| SQLite | 任务上下文、去重键、通知状态 |
| `/data/downloads` | 下载目录，aria2 写入；bot 只读或做空间检查 |

### 5.2 推荐运行方式

- NapCat、bot、aria2 在同一个 Docker Compose 网络中。
- NapCat 以 Reverse WebSocket 方式连接 bot。
- aria2 的 JSON-RPC 只暴露给 bot，不直接暴露到公网。
- bot 的 OneBot WS 端口只对 NapCat 开放。
- 如果 NapCat 在宿主机运行，则使用 `ws://host.docker.internal:8080/...` 或宿主机内网 IP。

---

## 6. OneBot v11 接入设计

### 6.1 连接方式选择

| 方式 | 方向 | 优点 | 缺点 | 结论 |
|---|---|---|---|---|
| Reverse WebSocket | NapCat → bot | 一条连接收发，NapCat 自动重连，无需公网 IP | bot 需要实现 WS 服务端 | **推荐** |
| Forward WebSocket | bot → NapCat | bot 主动连接，NapCat 只做服务端 | bot 需要自己实现重连 | 可用 |
| HTTP Webhook | NapCat → bot，bot → NapCat | 调试直观，curl 即可测试 | 两条链路、两套 token、发送延迟略高 | 可选 |

### 6.2 NapCat 配置示例

以下为示意，字段名和目录以当前 NapCat 版本为准：

```json
{
  "network": {
    "websocketClients": [
      {
        "name": "qq-aria2-dl",
        "enable": true,
        "url": "ws://127.0.0.1:8080/onebot/v11/ws",
        "messagePostFormat": "array",
        "reportSelfMessage": false,
        "token": "change-me",
        "heartInterval": 30000
      }
    ]
  }
}
```

### 6.3 bot 侧 WS 处理

- 暴露 `/onebot/v11/ws`。
- 校验 token：
  - `Authorization: Bearer <token>`
  - 或 NapCat 支持的 `access_token` 查询参数
  - 使用 `hmac.compare_digest` 做常量时间比较。
- 校验失败直接关闭连接，不进入业务逻辑。
- 只保留一个活跃连接；NapCat 重连时关闭旧连接并替换。
- 处理 `meta_event` 心跳；超过配置时间没有心跳则判定连接失效。
- 忽略 `post_type == "message_sent"`，避免机器人自己触发自己。
- 可选处理 `notice`，例如群成员变动、消息撤回；第一版可以忽略。

### 6.4 action 调用与 echo 匹配

OneBot v11 的 action 请求：

```json
{
  "action": "send_group_msg",
  "params": {
    "group_id": 123456,
    "message": [
      {"type": "text", "data": {"text": "已加入下载队列"}}
    ]
  },
  "echo": "uuid"
}
```

bot 侧维护：

```python
pending: dict[str, asyncio.Future] = {}

async def call(action: str, **params):
    echo = uuid4().hex
    fut = asyncio.get_running_loop().create_future()
    pending[echo] = fut

    await ws.send(json.dumps({
        "action": action,
        "params": params,
        "echo": echo,
    }))

    try:
        return await asyncio.wait_for(fut, timeout=10)
    finally:
        pending.pop(echo, None)
```

收到响应后按 `echo` 找到 `Future`，检查 `status`/`retcode`，再把结果返回给调用方。

### 6.5 消息段解析

建议 NapCat 使用 `messagePostFormat: array`，bot 直接解析数组：

- `text`：命令和链接来源。
- `at`：判断是否 @ 机器人。
- `reply`：回复某条消息时使用。
- `image`：第一版忽略。
- `json` / `xml`：QQ 卡片消息，后续再解析其中可能存在的链接。

命令解析前先拼接所有 `text` 段；如果要支持“@机器人 /dl ...”，先过滤 `at` 段，再解析文本。

---

## 7. 消息处理流程

```text
收到 message 事件
  → 过滤自己发送的消息和非法事件
  → 识别群聊/私聊
  → ACL 鉴权
  → 解析命令或自动链接
  → 提取并规范化 URL
  → 去重检查
  → 磁盘空间预检查
  → 生成任务并调用 aria2.addUri
  → 写入 SQLite
  → 回复来源会话
```

### 7.1 命令设计

| 命令 | 说明 |
|---|---|
| `/dl <url> [name]` | 手动添加下载任务 |
| `/stat [gid]` | 查看任务详情；不填 gid 时查看最近任务 |
| `/progress [gid]` / `/p [gid]` | 查询任务进度；不填 gid 时查看当前会话的进行中任务 |
| `/cancel <gid>` | 取消任务 |
| `/ls` | 列出当前用户/当前群可见的任务 |
| `/help` | 显示帮助 |
| `/pause <gid>` | 暂停（可选，第二阶段） |
| `/resume <gid>` | 恢复（可选，第二阶段） |

第一版建议做 `/dl`、`/progress`、`/stat`、`/cancel`、`/ls`、`/help`。

### 7.2 进度查询规则

#### 命令与范围

- `/progress [gid]`：查询任务进度；`/p` 可作为快捷别名。
- `/stat [gid]`：查询任务详情，与 `/progress` 共用同一份状态快照。
- 不带 gid 时：
  - 私聊：查询该用户最近 N 个未完成任务。
  - 群聊：普通成员只查询自己提交的任务，管理员可以查询当前群全部任务。
  - 默认最多展示 5 个任务，超过时提示使用 `/ls`。
- 带 gid 时：必须校验任务属于当前会话/用户，禁止跨群或跨用户查看。

#### 数据来源与缓存

- 对账循环每 2 秒更新一次内存中的 `progress_cache[gid]`。
- 不带 gid 且缓存为空时，可以用 `aria2.tellActive` / `tellWaiting` 拉取进行中任务，再按会话和用户过滤。
- `/progress` 优先读取缓存；缓存超过 5 秒，或任务状态不明时，再实时调用 `aria2.tellStatus` 刷新。
- 至少读取以下字段：
  - `status`
  - `totalLength`
  - `completedLength`
  - `downloadSpeed`
  - `connections`
  - `files`
  - `errorMessage`
- 不把每次进度写入 SQLite，只持久化最终任务状态和通知状态，避免高频写库。

#### 计算规则

- 百分比：
  - `totalLength > 0`：`completedLength / totalLength * 100`，保留 1 位小数。
  - `totalLength == 0`：显示“获取元数据中”，不显示百分比。
- 剩余时间：
  - `downloadSpeed > 0`：`(totalLength - completedLength) / downloadSpeed`。
  - 小于 1 分钟显示秒数，超过 1 小时显示“X 小时 Y 分钟”；无法计算时显示“未知”。
- 大小和速度统一使用 B、KiB、MiB、GiB、TiB 格式化。
- 进度条固定宽度，例如 20 格：
  - `[████████████░░░░░░░░] 60.0%`
  - 不支持 Unicode 的环境可退化为 `[############--------]`。
- 状态映射：
  - `active` → 下载中
  - `waiting` → 排队中
  - `paused` → 已暂停
  - `complete` → 已完成
  - `error` → 失败
  - `removed` → 已取消
- 多文件任务使用 aria2 的聚合 `totalLength` / `completedLength`；文件过多时只显示文件数量和一个代表文件名。
- `error` 状态展示 `errorMessage` 的简短版本，完整错误写入日志。

#### 展示模板

单个任务：

```text
📥 下载中
文件：example.zip
进度：[████████████░░░░░░░░] 60.0%
大小：1.20 GiB / 2.00 GiB
速度：3.45 MiB/s
剩余：约 2 分 18 秒
连接：8
gid：0123456789abcdef
```

多个任务：

```text
当前任务（最多显示 5 个）：
1. example.zip  [██████░░░░] 60.0%  3.45 MiB/s
2. movie.mkv    [███░░░░░░░] 30.2%  1.20 MiB/s
...
使用 /progress <gid> 查看详情，或 /ls 查看完整列表。
```

已完成/失败：

```text
✅ 已完成：example.zip（2.00 GiB）
❌ 失败：example.zip（原因：...）
```

#### 查询频率限制

- 每用户/每群 3 秒内最多查询 1 次；超出时返回缓存结果或提示“查询过于频繁”。
- 同一 gid 的实时 RPC 查询结果缓存 2 秒，避免连续查询打满 aria2。
- 进度查询不触发下载、不改变任务状态。
- 默认不主动推送进度，避免 QQ 风控和刷屏；后续可以配置“每 30 秒或每 10% 推送一次”，但默认关闭。

### 7.3 自动下载模式

按群配置开启，不建议全局默认开启：

```text
群消息
  → 群在 whitelist 且 auto_download = true
  → 可选要求 @机器人
  → 提取消息中的 URL
  → 每条消息最多处理 N 个 URL
  → 逐个执行去重、空间检查和提交
```

注意：

- 不要处理机器人自己发送的消息。
- 同一群里同一 URL 在 TTL 内只提交一次。
- 自动模式下重复链接可以静默忽略，避免刷屏。
- 手动 `/dl` 遇到重复链接时可以回复“已存在/刚刚已提交”。
- 卡片消息中的链接不一定在 `text` 段，第一版先支持文本链接，后续增加 `json/xml` 解析。

### 7.4 URL 提取与规范化

支持的 scheme：

- `http://`
- `https://`
- `magnet:?xt=...`（如果启用 BitTorrent）

建议流程：

1. 用正则提取候选 URL。
2. 去掉尾部中文标点、括号、引号等常见误伤字符。
3. 使用 `urllib.parse.urlsplit` 规范化：
   - scheme 和 host 转小写。
   - 去掉 fragment。
   - 补默认端口。
   - 是否清理 tracking 参数由配置决定；默认不要过度清理，避免破坏签名 URL。
4. 限制 URL 长度，例如 8192 字符。
5. 拒绝 `file://`、`ftp://` 等未明确允许的协议。

---

## 8. QQ 白名单与权限设计

### 8.1 权限层级

| 层级 | 说明 |
|---|---|
| 群白名单 | 只有配置中的群才会被处理 |
| 私聊白名单 | 只有配置中的 QQ 用户才能私聊使用 |
| 群用户白名单 | 某些群只允许指定用户触发下载 |
| 管理员 | 可以执行取消他人任务、查看全局状态、恢复磁盘守卫等操作 |

默认策略：**未配置的群/私聊一律拒绝**。

### 8.2 群配置示例

```toml
[groups."123456"]
enabled = true
auto_download = true
require_at = false
allowed_users = [111111, 222222]
max_urls_per_message = 3
max_concurrent = 2
```

`allowed_users` 为空时可以表示“群内所有成员可用”，也可以表示“仅管理员可用”；建议在配置校验阶段明确一个语义，避免歧义。第一版推荐：

- `allowed_users = []`：只有管理员可以手动下载。
- `allowed_users = ["*"]`：群内所有成员可用。
- 显式列表：仅列表内用户可用。

### 8.3 私聊配置

```toml
[private]
enabled = true
allowed_users = [111111, 222222]
```

私聊比群聊更容易被滥用，建议默认只允许管理员或固定用户。

### 8.4 限流

建议至少有两层：

- 每用户每 10 分钟最多提交 N 个任务。
- 每群每 10 分钟最多提交 M 个任务。

第一版可以使用进程内 token bucket / 滑动窗口，重启后清空可接受。若要求重启后仍保持限额，再把计数落到 SQLite。

---

## 9. 去重设计

### 9.1 去重目标

- 防止同一个人连续发送同一链接。
- 防止群里多人转发同一链接导致重复下载。
- 防止 bot 重连、消息重复投递时重复提交。
- 防止自动下载模式下同一消息被处理多次。

### 9.2 去重键

推荐按“会话 + URL”去重：

```text
dedupe_key = sha256(chat_type + ":" + chat_id + ":" + canonical_url)
```

可选 scope：

- `chat`：同一群/私聊内去重（默认，推荐）。
- `global`：所有群和私聊共享去重。
- `user`：同一用户内去重。

TTL 建议默认 10 分钟，可配置。TTL 到期后允许再次下载同一 URL。

### 9.3 防并发竞态

两条消息几乎同时到达时，不能只做“先查再插”。推荐组合：

1. 进程内 `asyncio.Lock`，按 dedupe key 加锁。
2. SQLite `dedupe_keys` 表使用 `PRIMARY KEY` 或 `UNIQUE`。
3. 使用 `INSERT OR IGNORE` 抢占去重键；只有插入成功的一方继续提交。
4. 如果 `addUri` 最终失败，删除该去重键，允许用户立即重试。

SQLite 是跨重启的兜底；进程内锁负责降低并发冲突。

### 9.4 去重清理

- 每次 reconcile 循环顺带删除 `expires_at < now` 的记录。
- 或每小时清理一次。
- 不要无限增长。

### 9.5 消息级幂等

除 URL 去重外，还应记录最近处理过的 `message_id`：

- 防止 OneBot 重连或异常重投导致同一消息处理两次。
- 可以放内存 TTL 缓存；若要求严格，落 SQLite。
- `message_id` 建议按字符串存储，兼容不同实现。

---

## 10. 磁盘空间与下载约束

### 10.1 配置项

```toml
[disk]
download_dir = "/data/downloads"
min_free_bytes = 5368709120       # 保留 5 GiB
min_free_percent = 10              # 保留磁盘总量的 10%
critical_free_bytes = 2147483648   # 低于 2 GiB 进入 critical
unknown_size_reserve_bytes = 536870912
head_timeout_sec = 5
```

实际保留空间取 `min_free_bytes` 和 `total * min_free_percent / 100` 的较大值。

### 10.2 提交前检查

```text
free = shutil.disk_usage(download_dir).free
reserve = max(min_free_bytes, total * min_free_percent / 100)
estimated = HEAD Content-Length（如果可获取）
         否则 unknown_size_reserve_bytes
if free - estimated < reserve:
    拒绝任务，并告知用户当前空间不足
```

注意：

- `Content-Length` 不一定存在，尤其是磁力链、分块传输、重定向后的资源。
- HEAD 请求要设置超时，不能让消息处理卡死。
- 不要对所有 URL 强制 HEAD；失败时按未知大小处理。
- 多个任务同时提交时，需要维护进程内 `reserved_bytes`，否则每个请求都看到同样的剩余空间，会一起放行。

### 10.3 下载过程中的磁盘守卫

aria2 没有可靠的“单文件最大大小”通用限制，必须在运行期监控：

- reconcile 循环或独立的 disk guard 每 5 秒检查一次剩余空间。
- 低于 `critical_free_bytes` 时：
  - 调用 `aria2.pauseAll()` 暂停下载。
  - 记录 `disk_guard_state = critical`。
  - 通知管理员。
- 空间恢复到安全线以上后：
  - 默认要求管理员手动 `/resume`；也可以配置自动恢复。
  - 使用滞回阈值，例如恢复到 `reserve * 1.2` 以上才恢复，避免反复抖动。

### 10.4 文件清理

第一版不建议自动删除用户文件。可以提供：

- 手动清理命令（管理员）。
- 按保留天数清理的配置项，但默认关闭。
- 下载完成通知中附带文件路径和大小，方便用户自行处理。

---

## 11. 任务模型与状态机

### 11.1 本地任务状态

```text
accepted
  → submitting
  → submitted
  → downloading
  → completed
  → failed
  → cancelled
```

建议将本地状态与 aria2 原始状态分开：

- `status`：bot 的业务状态。
- `aria2_status`：aria2 返回的 `active`、`waiting`、`paused`、`complete`、`error`、`removed`。

### 11.2 状态转换

```text
用户提交
  → accepted
  → submitting（已生成 gid，准备调用 addUri）
  → submitted（addUri 成功）
  → downloading（aria2 active）
  → completed / failed
  → notified（通知已发送）
```

异常路径：

- `addUri` 失败：`failed`，并释放去重键。
- 用户取消：`cancelled`，调用 aria2 remove。
- aria2 重启后 gid 找不到：标记 `unknown`，尝试按 URL/out path 对账；无法恢复则通知用户。
- 通知发送失败：保持未通知，按退避策略重试。

### 11.3 GID 设计

aria2 的 GID 是 16 位十六进制字符串，可以手动指定。建议：

```python
gid = secrets.token_hex(8)  # 16 hex chars
```

调用 `aria2.addUri` 时把 `gid` 放进 options。这样：

- 任务创建前就能把 gid 写入 SQLite。
- 便于日志关联。
- 配合 aria2 的 `--save-session`，重启后更容易恢复映射。

注意：GID 必须唯一，且不能全为 0。

---

## 12. aria2 集成设计

### 12.1 RPC 连接

- 默认使用 HTTP JSON-RPC：`http://127.0.0.1:6800/jsonrpc`
- 配置 `rpc-secret`。
- 使用 `httpx.AsyncClient`，不要用同步 `requests` 阻塞事件循环。
- 设置连接超时和读取超时，例如 5 秒。
- RPC 失败时记录日志并做有限重试，不要把异常直接抛给 QQ 用户。

### 12.2 常用方法

| 方法 | 用途 |
|---|---|
| `aria2.addUri` | 添加 HTTP/HTTPS/magnet 任务 |
| `aria2.tellStatus` | 查询单个任务的进度与状态 |
| `aria2.tellActive` | 查询活动任务 |
| `aria2.tellWaiting` | 查询等待任务 |
| `aria2.tellStopped` | 查询已完成/失败任务 |
| `aria2.pause` / `unpause` | 暂停/恢复单任务 |
| `aria2.pauseAll` / `unpauseAll` | 磁盘守卫使用 |
| `aria2.remove` / `forceRemove` | 取消任务 |
| `aria2.purgeDownloadResult` | 清理 aria2 的已完成结果 |
| `aria2.getGlobalStat` | 健康检查和全局状态 |

进度查询和状态对账主要使用 `aria2.tellStatus`，至少读取 `status`、`totalLength`、`completedLength`、`downloadSpeed`、`connections`、`files` 和 `errorMessage`。

### 12.3 addUri 选项

示意：

```json
{
  "dir": "/data/downloads",
  "out": "safe-filename.ext",
  "gid": "0123456789abcdef",
  "max-connection-per-server": "4",
  "split": "4",
  "continue": "true",
  "max-tries": "3",
  "retry-wait": "10",
  "user-agent": "Mozilla/5.0 ...",
  "referer": "https://example.com/"
}
```

注意：

- `out` 必须经过文件名清洗，不能包含路径分隔符。
- `user-agent` / `referer` 是否允许用户自定义，取决于群的信任级别。
- 磁力链没有固定文件名时，不要强行设置 `out`。

### 12.4 aria2 配置建议

```conf
enable-rpc=true
rpc-listen-all=false
rpc-secret=change-me
rpc-listen-port=6800
continue=true
max-concurrent-downloads=3
max-connection-per-server=4
split=4
max-tries=3
retry-wait=10
save-session=/data/aria2/session.txt
input-file=/data/aria2/session.txt
save-session-interval=30
```

`rpc-listen-all=false` 表示只监听本地回环；Docker 场景中应只让 RPC 端口在内部网络可达。

### 12.5 完成事件策略

优先级：

1. **MVP：周期性对账轮询**，简单、可靠、可恢复。
2. **增强：aria2 WebSocket 通知**，降低延迟。
3. **不推荐作为唯一方案：on-download-complete hook 脚本**，会额外引入脚本、重试和安全问题。

即使启用了 aria2 WebSocket 通知，也保留轮询作为兜底。

---

## 13. 完成通知与对账

### 13.1 对账循环

每 2 秒执行一次：

```text
读取 SQLite 中 notified_at IS NULL 且 gid 不为空的任务
  → 批量或逐个调用 aria2.tellStatus
  → 更新 status / aria2_status
  → 如果状态为 complete/error/removed：
       → 组装通知
       → 发送到来源群/私聊
       → 成功后写入 notified_at
  → 如果发送失败：
       → 增加 attempts
       → 记录错误
       → 下一轮重试
```

每轮同时更新内存中的 `progress_cache[gid]`，供 `/progress` 使用。

优化：

- 只查询未完成任务和未通知任务，不要每轮全表扫描。
- 任务多时使用 `system.multicall` 批量查询。
- aria2 RPC 不可用时指数退避，避免日志刷屏。
- 每轮顺带清理过期去重键。

### 13.2 启动恢复

bot 启动时：

1. 初始化 SQLite。
2. 连接 aria2，确认 RPC 可用。
3. 先执行一次 reconcile，处理未通知任务。
4. 再开始接受 OneBot 消息。

如果 NapCat 尚未连接，不影响对账；通知保持在待发送状态，等连接恢复后重试。

### 13.3 通知幂等

- 使用 `notified_at` 标记成功通知。
- 发送成功后再写 `notified_at`；如果写库失败，可能重复发送一次，这是可接受的“至少一次”语义。
- 若需要更严格，可增加 `notification_state`（`pending/sending/sent`）和超时回收，但第一版不必过度设计。
- 连续失败超过 N 次后，记录 `notify_failed` 并通知管理员，避免无限重试。

### 13.4 通知内容

成功：

```text
✅ 下载完成
文件：example.zip
大小：1.2 GiB
耗时：3 分 12 秒
gid：0123456789abcdef
```

失败：

```text
❌ 下载失败
文件/链接：...
原因：...
gid：...
```

发送时：

- 群聊使用 `send_group_msg`，并尽量带 `reply` 段回复原消息。
- 私聊使用 `send_private_msg`。
- 原消息被撤回导致 reply 失败时，退化为普通消息。
- 只通知来源会话，不做全局广播。

---

## 14. 数据存储设计

### 14.1 SQLite 表结构

```sql
PRAGMA journal_mode = WAL;
PRAGMA busy_timeout = 5000;

CREATE TABLE IF NOT EXISTS tasks (
    gid                     TEXT PRIMARY KEY,
    chat_type               TEXT NOT NULL CHECK (chat_type IN ('group', 'private')),
    chat_id                 INTEGER NOT NULL,
    user_id                 INTEGER NOT NULL,
    message_id              TEXT,
    url                     TEXT NOT NULL,
    out_name                TEXT,
    status                  TEXT NOT NULL DEFAULT 'accepted',
    aria2_status            TEXT,
    error_message           TEXT,
    created_at              INTEGER NOT NULL,
    updated_at              INTEGER NOT NULL,
    finished_at             INTEGER,
    notified_at             INTEGER,
    notification_attempts   INTEGER NOT NULL DEFAULT 0,
    last_notification_error TEXT
);

CREATE INDEX IF NOT EXISTS idx_tasks_pending_notify
    ON tasks(notified_at)
    WHERE notified_at IS NULL;

CREATE TABLE IF NOT EXISTS dedupe_keys (
    key         TEXT PRIMARY KEY,
    chat_type   TEXT NOT NULL,
    chat_id     INTEGER NOT NULL,
    url         TEXT NOT NULL,
    created_at  INTEGER NOT NULL,
    expires_at  INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_dedupe_expires
    ON dedupe_keys(expires_at);
```

### 14.2 存储访问

- 使用 `aiosqlite`，或把同步 `sqlite3` 操作放进 `asyncio.to_thread`。
- 所有写操作使用短事务。
- 单进程单写者，WAL 模式足够。
- 使用 `PRAGMA user_version` 或 migrations 目录管理 schema 版本。
- 不把大文件内容存入数据库，只存路径和元数据。

### 14.3 数据保留

- 任务记录默认保留 30 天，可配置。
- 去重键按 TTL 清理。
- 已完成任务的 aria2 结果可以定期 `purgeDownloadResult`，但 SQLite 记录保留用于查询。
- 日志中不要记录完整 token、cookie 或带敏感参数的 URL。

---

## 15. 配置设计

建议使用 TOML，配合环境变量覆盖敏感项。

```toml
[app]
host = "0.0.0.0"
port = 8080
log_level = "INFO"

[onebot]
path = "/onebot/v11/ws"
token = "change-me"
heartbeat_timeout_sec = 90
send_timeout_sec = 10

[aria2]
rpc_url = "http://127.0.0.1:6800/jsonrpc"
secret = "change-me"
poll_interval_sec = 2
download_dir = "/data/downloads"
max_concurrent = 3

[progress]
cache_ttl_sec = 5
query_cooldown_sec = 3
rpc_cache_sec = 2
max_tasks_per_query = 5

[storage]
db_path = "/data/bot.db"
task_retention_days = 30

[dedupe]
ttl_sec = 600
scope = "chat"

[disk]
min_free_bytes = 5368709120
min_free_percent = 10
critical_free_bytes = 2147483648
unknown_size_reserve_bytes = 536870912
head_timeout_sec = 5

[rate_limit]
per_user_per_10min = 3
per_group_per_10min = 10

[private]
enabled = true
allowed_users = [111111]

[groups."123456"]
enabled = true
auto_download = true
require_at = false
allowed_users = ["*"]
max_urls_per_message = 3
```

建议：

- `ONEBOT_TOKEN`、`ARIA2_SECRET` 通过环境变量注入，不写进仓库。
- 启动时用 pydantic 做完整校验，配置错误直接退出。
- 对 `allowed_users` 的 `[]` 和 `["*"]` 语义做明确注释。
- 为每个群提供独立配置，避免全局开关导致误下载。
- 进度查询的缓存、频率和展示条数由 `[progress]` 控制，与下载任务限流分开配置。

---

## 16. 安全设计

### 16.1 网络边界

- OneBot WS 只对 NapCat 可达，不直接暴露公网。
- aria2 RPC 只对 bot 可达，开启 `rpc-secret`。
- 如果必须跨主机部署，使用内网、VPN 或 TLS 反向代理。
- 不在日志中输出 token 和 secret。

### 16.2 QQ 侧安全

- 群/私聊白名单默认拒绝。
- 管理员命令单独鉴权。
- 每用户/每群限流。
- 不要自动同意陌生好友请求或群邀请。
- 避免高频群发，减少 QQ 风控风险。

### 16.3 URL 安全

- 只允许明确支持的 scheme。
- 限制 URL 长度、数量。
- 对不可信群考虑：
  - 域名 allowlist。
  - 解析 DNS，拒绝 loopback、私网、link-local 地址。
  - 防止重定向绕过（aria2 最终解析地址，必要时使用下载代理或更严格的网络隔离）。
- 可信内部群可以先放宽，但要在文档中明确风险。

### 16.4 文件名安全

- 使用 `Path(name).name` 取 basename。
- 拒绝空名、`.`、`..`、包含路径分隔符的名字。
- 限制长度，保留扩展名。
- 不信任用户提供的 `out` 直接传给 aria2。
- 磁力链/种子任务不强行指定文件名。

### 16.5 资源安全

- 磁盘剩余空间守卫。
- 最大并发下载数。
- 可选单任务限速。
- 任务数量上限和队列长度上限。
- 定期清理过期去重键和旧任务记录。

---

## 17. 可靠性与错误处理

### 17.1 OneBot 连接

- Reverse WS 断开后，NapCat 自动重连；bot 接受新连接并替换旧连接。
- action 调用设置超时，超时后清理 pending Future。
- 发送失败区分：
  - 连接断开：等待重连后重试。
  - QQ 返回 retcode 错误：记录并决定是否重试。
  - 频率限制：退避后重试。
- 出站消息使用小队列，避免瞬间刷屏。

### 17.2 aria2

- RPC 连接失败：指数退避，健康检查标记 degraded。
- addUri 失败：向用户返回明确原因；删除去重键，允许重试。
- 下载失败：由 aria2 自身重试；最终失败后通知用户。
- aria2 重启：依赖 session 恢复；bot 对账未完成任务。
- gid 丢失：按 URL/out path 尝试匹配；无法匹配时通知管理员。

### 17.3 SQLite

- WAL 模式 + busy timeout。
- 写操作失败时记录日志，不要把数据库异常暴露给 QQ 用户。
- 启动时执行 schema migration。
- 定期备份 `bot.db` 为可选运维项。

### 17.4 优雅关闭

- 停止接受新消息。
- 关闭 OneBot WS。
- 取消 reconcile/disk guard 后台任务。
- 关闭 httpx client。
- 确保 SQLite 连接关闭。
- 不强制中断正在进行的 aria2 下载。

---

## 18. 可观测性

### 18.1 日志

建议结构化日志，至少包含：

- 事件类型
- `message_id`
- `chat_type` / `chat_id` / `user_id`
- `gid`
- action 名称
- aria2 状态变化
- 耗时
- 错误码和错误信息

示例字段：

```json
{
  "event": "download_completed",
  "gid": "0123456789abcdef",
  "chat_type": "group",
  "chat_id": 123456,
  "user_id": 111111,
  "duration_ms": 192000,
  "size": 1288490188
}
```

### 18.2 健康检查

提供 `GET /healthz`：

- SQLite 可读写。
- aria2 `getGlobalStat` 成功。
- OneBot WS 是否已连接。
- 磁盘空间是否处于 critical。

返回 200 或 503，并附带简要状态。Docker healthcheck 可以使用这个端点。

### 18.3 管理员告警

以下情况通知管理员：

- 磁盘进入 critical。
- aria2 连续不可用。
- 通知发送连续失败。
- 数据库写入失败。
- NapCat 长时间未连接。

---

## 19. 部署设计

### 19.1 Docker Compose 草案

```yaml
services:
  napcat:
    # 镜像和登录配置按实际 NapCat 版本填写
    volumes:
      - ./data/napcat:/app/napcat/config
    restart: unless-stopped

  aria2:
    # 使用 aria2 镜像或自建镜像
    volumes:
      - ./data/downloads:/data/downloads
      - ./data/aria2:/data/aria2
    restart: unless-stopped

  bot:
    build: .
    volumes:
      - ./data/downloads:/data/downloads
      - ./data/bot:/data/bot
    environment:
      - ONEBOT_TOKEN=change-me
      - ARIA2_SECRET=change-me
    restart: unless-stopped
```

要点：

- NapCat 登录态必须用 volume 持久化，否则每次重启都要重新登录。
- aria2 的 session 文件和下载目录都要持久化。
- bot 与 aria2 共享下载目录，便于空间检查和后续扩展。
- `depends_on` 不代表服务已就绪，bot 内部需要重试连接。
- bot 使用单 worker 运行，例如 `uvicorn --workers 1`。

### 19.2 非 Docker 部署

如果直接跑在宿主机：

- NapCat、aria2、bot 分别由 systemd/supervisor 管理。
- 为每个进程配置自动重启。
- 日志输出到 journald 或文件。
- 注意 Windows 下路径、文件锁和盘符空间检查的差异。

---

## 20. 推荐目录结构

```text
qq-aria2-dl/
├─ DESIGN.md
├─ README.md
├─ pyproject.toml
├─ .env.example
├─ config.example.toml
├─ docker-compose.yml
├─ src/
│  └─ qq_aria2_dl/
│     ├─ __init__.py
│     ├─ main.py
│     ├─ config.py
│     ├─ logging.py
│     ├─ models.py
│     ├─ onebot/
│     │  ├─ __init__.py
│     │  ├─ server.py
│     │  ├─ client.py
│     │  ├─ events.py
│     │  └─ messages.py
│     ├─ bot/
│     │  ├─ __init__.py
│     │  ├─ router.py
│     │  ├─ acl.py
│     │  ├─ urls.py
│     │  ├─ rate_limit.py
│     │  └─ tasks.py
│     ├─ aria2/
│     │  ├─ __init__.py
│     │  ├─ rpc.py
│     │  └─ reconcile.py
│     └─ storage/
│        ├─ __init__.py
│        ├─ db.py
│        └─ migrations/
├─ tests/
│  ├─ test_urls.py
│  ├─ test_acl.py
│  ├─ test_dedupe.py
│  ├─ test_tasks.py
│  └─ test_reconcile.py
└─ scripts/
```

第一版不必一次性建满所有文件，可以按里程碑逐步增加。

---

## 21. 开发里程碑

### Phase 0：项目骨架

- [ ] 初始化 `pyproject.toml` 和依赖。
- [ ] 配置加载与校验。
- [ ] 日志初始化。
- [ ] FastAPI/uvicorn 启动和 `/healthz` 空实现。
- [ ] SQLite 初始化与 migration 框架。

### Phase 1：OneBot 接入

- [ ] Reverse WebSocket 端点。
- [ ] token 校验。
- [ ] 心跳和断线处理。
- [ ] action 调用与 echo Future 匹配。
- [ ] 收消息、解析文本、回复 `pong`。
- [ ] 忽略 `message_sent` 和自身消息。
- [ ] 用假 WS 客户端写集成测试。

### Phase 2：aria2 基础能力

- [ ] HTTP JSON-RPC client。
- [ ] `addUri`、`tellStatus`、`remove`、`getGlobalStat`。
- [ ] `/dl` 添加任务。
- [ ] `/stat` 查看任务详情。
- [ ] `/progress` 查询百分比、速度、剩余时间和状态。
- [ ] `/cancel` 取消任务。
- [ ] 文件名清洗。
- [ ] 明确 addUri 失败时的用户提示。

### Phase 3：任务状态与通知

- [ ] 任务表、gid 生成与保存。
- [ ] 2 秒 reconcile 循环。
- [ ] 完成/失败通知。
- [ ] `notified_at` 幂等。
- [ ] 启动时补发未通知任务。
- [ ] bot 重启恢复测试。
- [ ] aria2 重启对账测试。

### Phase 4：白名单、自动下载、去重、限流

- [ ] 群/私聊/用户白名单。
- [ ] 自动链接提取。
- [ ] URL 规范化与校验。
- [ ] 去重键、TTL、并发锁。
- [ ] 每条消息 URL 数量限制。
- [ ] 用户/群限流。
- [ ] 管理员命令。

### Phase 5：磁盘保护与安全加固

- [ ] 提交前空间检查。
- [ ] HEAD 大小探测和超时。
- [ ] 运行期磁盘守卫。
- [ ] critical 状态暂停所有下载并告警。
- [ ] SSRF/域名策略（按群信任级别配置）。
- [ ] 日志脱敏。
- [ ] 健康检查完善。

### Phase 6：部署与可选的增强

- [ ] Docker Compose。
- [ ] NapCat/aria2/bot 持久化 volume。
- [ ] healthcheck。
- [ ] 备份/恢复说明。
- [ ] 可选：aria2 WebSocket 事件。
- [ ] 可选：QQ 文件上传。
- [ ] 可选：后处理 Worker。
- [ ] 可选：Web 管理界面。

---

## 22. 测试计划

### 22.1 单元测试

- URL 提取、去尾标点、规范化。
- scheme 白名单。
- 文件名清洗和路径穿越防护。
- 群/私聊/用户白名单判断。
- `allowed_users` 的 `[]`、`["*"]`、显式列表语义。
- 去重 key 生成、TTL、过期清理。
- 并发提交同一 URL 只成功一个。
- token bucket 限流。
- 磁盘空间计算和滞回逻辑。
- 进度百分比、速度、剩余时间格式化。
- `totalLength = 0` 的元数据阶段展示。
- 多任务进度列表和查询限流。
- 配置校验。

### 22.2 集成测试

- Fake OneBot WS：
  - 发送群消息、私聊消息、心跳。
  - 记录 bot 发出的 action。
  - 模拟断线和重连。
  - 模拟错误 retcode。
- Fake aria2 RPC：
  - addUri 返回 gid。
  - tellStatus 返回 waiting/active/complete/error。
  - remove/pauseAll 调用记录。
- 临时 SQLite 数据库。
- 模拟 bot 重启后补发通知。
- 模拟 aria2 重启后任务丢失/恢复。

### 22.3 端到端测试

使用测试 QQ 账号和测试群：

- 白名单群自动下载。
- 非白名单群完全无响应。
- 私聊白名单可用、非白名单拒绝。
- 重复链接只下载一次。
- 取消任务后不再通知完成。
- 磁盘不足时拒绝新任务。
- 下载完成后通知只发一次。
- bot 重启后补发未通知消息。
- NapCat 重启后自动重连。
- 错误 token 被拒绝。
- 高频率消息触发限流。

### 22.4 测试工具

- `pytest` + `pytest-asyncio`。
- `httpx.MockTransport` 或本地 aiohttp 假服务模拟 aria2。
- 本地 WebSocket 假客户端模拟 NapCat。
- 可注入时钟，避免测试 TTL 时真正等待。
- 可注入磁盘用量函数，避免依赖真实磁盘。

---

## 23. MVP 验收标准

满足以下条件即可认为第一版可用：

- [ ] bot 以单进程运行，任务编排、进度查询和状态对账在进程内完成。
- [ ] NapCat 通过 Reverse WS 连接 bot，错误 token 被拒绝。
- [ ] 白名单群中的文本链接可自动提交 aria2。
- [ ] 私聊白名单用户可使用 `/dl`。
- [ ] 同群同 URL 在 TTL 内只提交一次。
- [ ] 磁盘低于安全线时拒绝新任务并给出原因。
- [ ] `/progress` 能显示百分比、速度、剩余时间和状态；`/stat` 能显示任务详情。
- [ ] `/cancel` 能取消任务。
- [ ] 下载完成/失败后向来源会话发送一次通知。
- [ ] bot 重启后能补发未发送通知。
- [ ] aria2 RPC 不可用时 bot 不崩溃，恢复后继续对账。
- [ ] `/healthz` 能反映 OneBot、aria2、SQLite 和磁盘状态。
- [ ] 日志包含 gid、chat_id、message_id 和错误原因。

---

## 24. 后续扩展方向

按优先级从高到低：

1. **aria2 WebSocket 事件**：降低完成通知延迟，轮询继续作为兜底。
2. **QQ 卡片消息链接解析**：解析 `json`/`xml` 消息段。
3. **下载后处理**：解压、重命名、上传网盘；如需独立执行，再增加后处理服务。
4. **文件回传 QQ**：`upload_group_file` / `upload_private_file`，注意大小限制和风控。
5. **Web 管理后台**：查看任务、磁盘、失败记录。
6. **多 QQ 账号/多 NapCat 实例**：需要区分 `self_id` 和连接。
7. **多实例部署**：需要单独设计共享状态和服务发现。
8. **OneBot v12 适配**：通过 transport adapter 隔离，不影响 TaskService。
9. **进度主动播报**：按配置每 30 秒或每 10% 推送一次；默认关闭，避免刷屏。

---

## 25. 待确认问题

开发前建议确认以下事项：

- [ ] 部署环境是 Windows 宿主机、Linux 宿主机，还是 Docker？
- [ ] NapCat 当前版本和网络配置方式？
- [ ] 只支持 HTTP/HTTPS，还是也要支持 magnet/BitTorrent？
- [ ] QQ 群是可信内部群还是公开群？这决定 SSRF 防护强度。
- [ ] 私聊是否只允许管理员？
- [ ] 单文件大小上限和总磁盘配额是多少？
- [ ] 下载文件是否需要保留期限或自动清理？
- [ ] 完成后是否需要把文件上传回 QQ？
- [ ] 是否需要 Web 管理界面？
- [ ] 是否有多账号或多实例需求？

这些问题不需要阻塞第一版开发，但会影响配置模型和安全策略的默认值。

---

## 26. 总结

这个项目的核心目标是把职责收敛为：

```text
OneBot 适配器
  + 任务编排
  + 进度查询与缓存
  + aria2 状态对账
  + SQLite 业务上下文
```

推荐的第一版技术栈：

```text
Python 3.11+
FastAPI + uvicorn（单 worker）
httpx
aiosqlite
pydantic-settings
NapCat + OneBot v11
aria2 JSON-RPC
```

第一版保持三个长期运行进程：NapCat、qq-aria2-dl、aria2c。先把“收消息 → 鉴权 → 去重 → 磁盘检查 → 提交 aria2 → 查询进度 → 对账 → 通知”这条主链路跑通，再考虑后处理、Web UI 和多实例。
