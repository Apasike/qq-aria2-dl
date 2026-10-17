# qq-aria2-dl

一个基于 **OneBot v11（NapCat）+ aria2** 的 QQ 下载推送机器人。

在 QQ 群 / 私聊中发送链接或 `/dl` 命令，bot 会把任务提交给 aria2 下载，下载完成 / 失败后自动向来源会话推送通知。

## 功能

- 接收 QQ 群消息和私聊消息（OneBot v11 反向 WebSocket）
- 下载权限按 **QQ 号白名单** 控制，管理员可运行时增删白名单
- 支持手动命令 `/dl <url> [文件名]`，以及群内自动识别链接下载（按群开关）
- 同链接去重，避免重复提交
- aria2 状态对账：bot 或 aria2 重启后自动补发未通知
- 进度查询：`/progress`、`/stat`，已完成任务自动过滤
- 磁盘空间保护（仅本地 aria2 场景）
- 健康检查：`GET /healthz`
- SQLite 持久化任务上下文、去重键、下载白名单

## 架构

```text
NapCat (OneBot v11)
   │  Reverse WebSocket
   ▼
qq-aria2-dl (FastAPI + asyncio, 单进程)
   │  HTTP JSON-RPC
   ▼
aria2c (可运行在本机或远程服务器)
   │
下载目录
```

## 快速开始

### 1. 安装

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
```

### 2. 配置

复制 `.env.example` 为 `.env`，至少修改：

```env
ONEBOT_TOKEN=你的 NapCat token
ARIA2_RPC_URL=http://你的aria2地址:6800/jsonrpc
ARIA2_SECRET=你的 aria2 rpc-secret
ADMIN_USERS=你的管理员QQ号
DOWNLOAD_ALLOWED_USERS=你的下载白名单QQ号
```

`ARIA2_DOWNLOAD_DIR` 建议留空，使用 aria2 自己配置的默认下载目录。

### 3. 启动 bot

```powershell
.\.venv\Scripts\python.exe -m qq_aria2_dl
```

### 4. 配置 NapCat 反向 WebSocket

```text
ws://127.0.0.1:8080/onebot/v11/ws?access_token=你的ONEBOT_TOKEN
```

## 下载白名单

下载权限按 QQ 号控制，不按群控制。白名单中的 QQ 号可以在任意群 / 私聊使用下载命令。

- 首次使用：在 `.env` 中配置 `ADMIN_USERS` 和 `DOWNLOAD_ALLOWED_USERS`。
- 运行时管理（白名单持久化在 SQLite）：

| 命令 | 说明 |
|---|---|
| `/wl list` | 查看下载白名单 |
| `/wl add <qq>` | 添加白名单（仅管理员） |
| `/wl del <qq>` | 删除白名单（仅管理员） |

## 命令

| 命令 | 说明 |
|---|---|
| `/dl <url> [文件名]` | 手动添加下载任务 |
| `/progress [gid]` | 查询进行中任务进度（`/p` 同义） |
| `/stat <gid>` | 查询任务详情 |
| `/cancel <gid>` | 取消任务 |
| `/ls` | 列出可见任务 |
| `/wl list/add/del` | 管理下载白名单 |
| `/help` | 显示帮助 |

群自动下载：在 `.env` 的 `GROUPS` 中为群配置 `auto_download=true` 后，白名单用户在该群直接发送文本链接即可自动下载。

## 配置说明

所有配置从 `.env` 读取（pydantic-settings）。

| 变量 | 说明 |
|---|---|
| `ONEBOT_TOKEN` | NapCat 反向 WS 鉴权 token |
| `ARIA2_RPC_URL` | aria2 JSON-RPC 地址 |
| `ARIA2_SECRET` | aria2 `rpc-secret` |
| `ARIA2_DOWNLOAD_DIR` | 下载目录；留空则使用 aria2 默认目录 |
| `ARIA2_SPLIT` | 单任务分段数，默认 `1` |
| `ARIA2_MAX_CONNECTION_PER_SERVER` | 单服务器连接数，默认 `1` |
| `DOWNLOAD_ALLOWED_USERS` | 下载白名单 QQ 号（逗号分隔） |
| `ADMIN_USERS` | 管理员 QQ 号（逗号分隔） |
| `GROUPS` | 群自动下载开关（JSON 对象） |
| `DEDUPE_SCOPE` | 去重范围：`chat` / `global` / `user` |

> `GROUPS` 示例：
>
> ```env
> GROUPS={"123456":{"enabled":true,"auto_download":true,"require_at":false,"allowed_users":[],"max_urls_per_message":3}}
> ```

## Docker 部署

aria2 和 bot 拆成两个 compose，可以分别部署在不同机器上。

### Dockerfile

```dockerfile
FROM python:3.11-slim

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src

RUN pip install --no-cache-dir .

ENV APP_HOST=0.0.0.0
EXPOSE 8080

CMD ["qq-aria2-dl"]
```

### 1. 部署 aria2（下载服务器上）

`docker-compose.aria2.yml`：

```yaml
services:
  aria2-pro:
    container_name: aria2-pro
    image: p3terx/aria2-pro
    network_mode: host
    environment:
      - PUID=1000
      - PGID=1000
      - UMASK_SET=022
      - RPC_SECRET=please-change-me
      - RPC_PORT=6800
      - LISTEN_PORT=6888
      - DISK_CACHE=64M
      - IPV6_MODE=false
      - TZ=Asia/Shanghai
    volumes:
      - ./data/aria2:/config
      - ./data/downloads:/downloads
    restart: unless-stopped
    logging:
      driver: json-file
      options:
        max-size: 1m

  ariang:
    container_name: ariang
    image: p3terx/ariang
    network_mode: host
    command: --port 6880
    restart: unless-stopped
    logging:
      driver: json-file
      options:
        max-size: 1m
```

启动：

```bash
docker compose -f docker-compose.aria2.yml up -d
```

### 2. 部署 bot（能访问 NapCat 和 aria2 的机器上）

`docker-compose.bot.yml`：

```yaml
services:
  bot:
    build: .
    container_name: qq-aria2-dl
    restart: unless-stopped
    env_file:
      - .env
    environment:
      APP_HOST: 0.0.0.0
      STORAGE_DB_PATH: /data/bot/bot.db
      ARIA2_RPC_URL: http://127.0.0.1:6800/jsonrpc
      ARIA2_SECRET: please-change-me
    ports:
      - "8080:8080"
    volumes:
      - ./data/bot:/data/bot
```

把 `ARIA2_RPC_URL` 改成 aria2 服务器的地址，`ARIA2_SECRET` 和 aria2 的 `RPC_SECRET` 保持一致，然后：

```bash
docker compose -f docker-compose.bot.yml up -d --build
```

NapCat 单独部署，反向 WebSocket 指向：

```text
ws://bot所在机器:8080/onebot/v11/ws?access_token=你的ONEBOT_TOKEN
```

## 健康检查

```bash
curl http://127.0.0.1:8080/healthz
```

返回 OneBot 连接、aria2、SQLite、磁盘状态；任一不健康时返回 `503`。

## 测试

```bash
.\.venv\Scripts\python.exe -m pytest
```

## 目录结构

```text
src/qq_aria2_dl/
├─ main.py          FastAPI 应用与启动入口
├─ config.py        .env 配置加载与校验
├─ models.py        ProgressSnapshot
├─ onebot/          OneBot v11 传输与 action 调用
├─ bot/             路由、ACL、URL 处理、限流、任务编排
├─ aria2/           aria2 JSON-RPC 客户端与状态对账
└─ storage/         SQLite 存储（任务、去重、白名单）
scripts/
└─ fake_onebot_client.py   本地冒烟测试用的假 NapCat 客户端
```

## License

MIT