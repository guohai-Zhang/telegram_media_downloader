# macOS 图形界面版 设计文档

- 日期：2026-09-23
- 分支：`feat/macos-gui`
- 状态：待审阅

## 1. 背景与目标

现在的使用方式是手改 `config.yaml`，再在终端里运行 `python media_downloader.py`。首次登录 Telegram 时，要在终端里输入手机号和验证码。网页（`module/web.py`，Flask）只在下载过程中提供进度查看和暂停，下载结束后进程退出，网页也随之消失。

本次要做一个**给非技术用户用的 Mac 应用**：双击 `.app` 打开一个窗口，在窗口里完成配置、登录、选频道、开始下载、查看进度，全程不用终端。

### 成功标准

1. 在一台没有安装 Python 的 Apple Silicon Mac（macOS 11 及以上）上，从 GitHub Releases 下载 `.dmg`，按 README 的步骤放行后能正常打开。
2. 首次打开后可以在窗口里依次完成：填写 API 凭证和代理 → 登录（验证码，以及可选的两步验证）→ 勾选频道 → 开始下载 → 实时查看进度。
3. 停止下载或关闭窗口后再次打开，能自动登录，并从上次的进度继续下载。
4. 命令行用法 `python media_downloader.py` 行为不变，现有测试全部通过。

## 2. 范围

**包含**
- 新的 GUI 入口 `gui_main.py`：pywebview 原生窗口 + 常驻的 Flask 服务 + 常驻的 Pyrogram 客户端
- 在网页里完成基础配置、登录和频道选择，并控制开始和停止
- arm64 `.app` / `.dmg` 的本机构建脚本，以及发布到 GitHub Releases 的流程说明

**不包含**（v1 明确不做）
- Intel Mac、通用二进制（universal2）
- Apple Developer ID 签名和公证
- 用 GitHub Actions 自动构建
- GUI 模式下的 bot 功能（即使 `config.yaml` 里有 `bot_token` 也不启动）
- 在界面里编辑高级配置项（过滤器、路径前缀、云盘上传、转发限制等），这些项继续从 `config.yaml` 读取并生效
- 频道头像，以及通过私有邀请链接（`t.me/+...`）加入频道
- Windows 和 Linux 的 GUI
- 多语言：新增的界面文字只做中文

## 3. 已确认的决策

| 问题 | 决定 |
|---|---|
| 目标用户 | 非技术用户：双击 `.app`，全程不碰终端 |
| 窗口形态 | pywebview 独立窗口（系统 WKWebView）。关闭窗口 = 停止下载并退出。同时提供浏览器访问地址 |
| 配置范围 | 基础项：API 凭证、代理、保存目录、媒体类型、同时下载文件数、频道。高级项保留在 `config.yaml` 里，界面保存时不覆盖 |
| API 凭证 | 每个用户去 my.telegram.org 申请自己的并填入，app 不内置任何凭证 |
| 并发数 | 界面上只提供"同时下载文件数"（`max_download_task`，1–10，默认 5）。`max_concurrent_transmissions` 不放进界面：没配置时仍按 ×5 自动计算，手动配置过的保持不动 |
| 选择频道 | 登录后从对话列表勾选，另外支持粘贴 `t.me/xxx` 或 `@xxx` |
| 实现方案 | 方案 A：单进程，常驻一个 Pyrogram 客户端，拆分 `main()` 复用下载逻辑 |
| 架构 | 只打 arm64 包 |
| 分发 | 本机构建，不签名（只做 ad-hoc 签名）。`.dmg` 上传到 `guohai-Zhang/telegram_media_downloader` 的 GitHub Releases |
| 监听地址 | GUI 模式强制使用 `127.0.0.1`，不受 `web_host` 配置影响 |

## 4. 架构

```
┌──────────────── TelegramDownloader.app（一个进程）────────────────┐
│                                                                   │
│  主线程            后台线程 1                  后台线程 2          │
│  ┌──────────┐     ┌─────────────────────┐    ┌──────────────┐    │
│  │ pywebview│────▶│ werkzeug server     │───▶│ app.loop     │    │
│  │ 原生窗口 │HTTP │ 127.0.0.1:port      │ ①  │ run_forever()│    │
│  └──────────┘     │ 现有路由 + /api/*   │    │ Pyrogram 客户端│    │
│       │ 关窗      └─────────────────────┘    │ worker × N   │    │
│       └──── controller.shutdown() ──────────▶│              │    │
│                                               └──────────────┘    │
│  ① run_coroutine_threadsafe(coro, app.loop).result(timeout)       │
└───────────────────────────────────────────────────────────────────┘
```

- macOS 的 Cocoa 要求窗口必须在主线程创建，所以 pywebview 占用主线程。
- `app.loop` 在后台线程里 `run_forever()`。所有 Pyrogram 调用都在这个事件循环里执行。
- Flask 线程通过 controller 提供的**同步方法**访问事件循环。异步和线程的边界只出现在 controller 里。

### 单元划分

| 单元 | 职责 | 依赖 |
|---|---|---|
| `gui_main.py`（新） | 数据目录、单实例锁、默认配置、启动线程、创建窗口、关窗处理 | controller、web、pywebview |
| `module/controller.py`（新） | 状态机，持有并重建客户端，登录各步骤，对话列表，开始/停止/关闭 | `module.app`、`media_downloader` 拆出的下载函数、pyrogram |
| `media_downloader.py` | 拆出 `start_download` 和 `stop_download`，`main()` 改为调用它们 | 与现在相同 |
| `module/download_stat.py` | 新增 `reset_download_stat()` | 与现在相同 |
| `module/app.py` | `assign_config` 允许缺少字段，有默认值 | 与现在相同 |
| `module/web.py` | 新增 `/api/*` 路由（只做参数校验和转发），token 校验，支持指定端口启动 | controller（GUI 模式下注入） |
| `module/templates/index.html` | 新增设置、账号、频道标签页，以及状态栏和页脚 | 无 |
| `packaging/macos/`（新） | spec、构建脚本、图标 | PyInstaller |

**依赖规则**：只有 `gui_main.py` 导入 `webview`，而且在 `main()` 函数内部延迟导入；`fcntl` 也在加锁的函数内部延迟导入（Windows 上没有这个模块）。`controller.py` 和 `web.py` 都不导入这两个模块，保证 Ubuntu 和 Windows 的 CI 能正常导入 `gui_main` 中的纯函数并测试。

## 5. 启动与数据目录

`gui_main.py` 启动顺序：

1. **数据目录**：`~/Library/Application Support/TelegramMediaDownloader/`，不存在就创建，然后 `os.chdir()` 进去。现有的 `config.yaml`、`data.yaml`、`sessions/`、`log/`、`temp/` 路径都基于 `os.path.abspath(".")`（`module/app.py:376-392`），`chdir` 之后不用改。从 Finder 启动时工作目录是 `/`，不 `chdir` 会因为没有写权限而崩溃。
2. **单实例锁**：对 `<数据目录>/.lock` 加 `fcntl.flock(LOCK_EX | LOCK_NB)`。加锁失败说明已有实例在运行，弹出原生提示后退出。这样可以避免两个进程同时打开同一个 SQLite session 文件。
3. **默认配置**：如果 `config.yaml` 不存在，写入：
   ```yaml
   api_id: ''
   api_hash: ''
   chat: []
   media_types: [audio, photo, video, document, voice, video_note]
   file_formats: {audio: [all], document: [all], video: [all]}
   save_path: <~/Downloads/Telegram 的绝对路径>
   file_path_prefix: [chat_title, media_datetime]
   ```
4. **加载配置**：`app.load_config()`。`assign_config` 改为用 `.get()` 读取 `api_id`、`api_hash`、`media_types`、`file_formats`，缺少时使用默认值（现在直接用下标读，缺字段会抛 KeyError，见 `module/app.py:447-452`）。CLI 的 `_check_config()` 另外显式检查 api_id 和 api_hash 不能为空，为空就报出明确的错误信息并返回 False，保持 CLI 现在"缺少凭证就不启动"的行为。
5. **日志**：写到 `<数据目录>/log/tdl.log`，沿用现有的 loguru 配置。
6. **事件循环线程**：`threading.Thread(target=app.loop.run_forever, daemon=True)`。
7. **Web 线程**：用 `werkzeug.serving.make_server("127.0.0.1", port, flask_app)` 启动。`port` 先尝试 `app.web_port`（默认 5000）；如果绑定失败（macOS 12 起，隔空播放接收器默认占用 5000 端口），就用端口 0，由系统分配。实际端口从 `server.server_port` 读取。
8. **token**：`secrets.token_urlsafe(16)`，每次启动生成一个新的。
9. **controller 初始化**：如果 api 凭证已经填写，就在后台自动连接（有 session 文件则直接进入 READY）。
10. **窗口**：`webview.create_window("Telegram 下载器", f"http://127.0.0.1:{port}/?token={token}", js_api=GuiApi(), width=1000, height=720)`，然后 `webview.start()`。

### 关窗处理

- 注册 `window.events.closing`。如果当前状态是 DOWNLOADING，就弹出原生确认框："正在下载，退出后进度会保存，下次可以继续。确定退出？"。用户选择取消时返回 False，阻止关闭。
- 确认关闭后调用 `controller.shutdown(timeout=10)`：停止下载 → `app.update_config()` → 断开客户端 → 停止 web 服务 → `app.loop.stop()`。

### `GuiApi`（pywebview `js_api`）

- `choose_folder() -> str | None`：弹出原生文件夹选择框。
- `open_log_folder()`：用 Finder 打开 `<数据目录>/log`。
- 在浏览器里访问时 `window.pywebview` 不存在，页面对应改为手动输入路径，并隐藏"打开日志文件夹"按钮。

## 6. Controller 状态机

### 状态

| 状态 | 含义 | 页面默认标签页 |
|---|---|---|
| `NEED_CONFIG` | api_id 或 api_hash 为空 | 设置 |
| `CONNECTING` | 正在创建客户端并 `connect()` | 账号（显示"连接中…"） |
| `ERROR` | 连接失败或出现未预期的异常，附带 `error` 文本 | 账号（显示原因和[重试]） |
| `LOGGED_OUT` | 已连接，但未授权 | 账号 |
| `CODE_SENT` | 验证码已发送，等待输入 | 账号 |
| `NEED_PASSWORD` | 需要两步验证密码，附带 `password_hint` | 账号 |
| `READY` | 已授权并完成初始化 | 没选频道时为频道，否则为下载中 |
| `DOWNLOADING` | 正在下载 | 下载中 |
| `STOPPING` | 正在停止 | 下载中 |

### 转换

```
NEED_CONFIG ──save_config(凭证齐全)──▶ CONNECTING
CONNECTING ──connect() 返回已授权──▶ 完成初始化 ──▶ READY
CONNECTING ──connect() 返回未授权──▶ LOGGED_OUT
CONNECTING ──异常或超时 20s（含 connect() 和登录初始化）──▶ ERROR ──retry()──▶ CONNECTING
LOGGED_OUT ──send_code(phone)──▶ CODE_SENT
CODE_SENT ──sign_in 成功──▶ 完成初始化 ──▶ READY
CODE_SENT ──SessionPasswordNeeded──▶ NEED_PASSWORD
CODE_SENT ──PhoneCodeExpired──▶ LOGGED_OUT
NEED_PASSWORD ──check_password 成功──▶ 完成初始化 ──▶ READY
READY ──start_download()──▶ DOWNLOADING ──全部完成──▶ READY
DOWNLOADING ──stop_download()──▶ STOPPING ──▶ READY
READY ──log_out()──▶ LOGGED_OUT
任意非下载状态 ──save_config(api_id/api_hash/proxy 有改动)──▶ 断开旧客户端 ──▶ CONNECTING
```

"完成初始化"指照着 Pyrogram 的 `Client.start()` 执行剩下的步骤：`invoke(raw.functions.updates.GetState())` → `client.me = await get_me()` → `await initialize()`。这里不调用 `start()`，因为 `start()` 会走交互式的 `authorize()`（读 stdin）。

### 客户端

- 用 `HookClient("media_downloader", api_id, api_hash, proxy, workdir=app.session_file_path, start_timeout=20, no_updates=True)` 创建，参数与 CLI 一致，所以 session 文件与 CLI 通用。
- api_id、api_hash、proxy 任意一项有改动，就断开并重建客户端。
- `phone`、`phone_code_hash` 只保存在 controller 的内存里。

### 对外方法（同步，线程安全）

所有方法先获取 `threading.Lock`，校验当前状态是否允许该操作（不允许就抛出 `InvalidState`，由 web 层转为 409），然后通过 `run_coroutine_threadsafe(...).result(timeout)` 执行异步部分。

| 方法 | 允许的状态 | 说明 |
|---|---|---|
| `status() -> dict` | 任意 | `state`、`error`、`password_hint`、`me`（用户名/名字）、`chat_count`、`progress`（总数/完成数） |
| `get_config() -> dict` | 任意 | 只返回基础项 |
| `save_config(form)` | 非 DOWNLOADING/STOPPING | 见第 8 节 |
| `retry()` | ERROR | 重新连接 |
| `send_code(phone)` | LOGGED_OUT、CODE_SENT | 允许重新发送 |
| `sign_in(code)` | CODE_SENT | |
| `check_password(password)` | NEED_PASSWORD | |
| `log_out()` | READY | `client.log_out()` 后重建客户端 → LOGGED_OUT |
| `list_dialogs(refresh)` | READY、DOWNLOADING | 缓存在内存中，`refresh=True` 时重新拉取 |
| `resolve_chat(link)` | READY | 解析 `t.me/xxx`、`https://t.me/xxx`、`@xxx`、`xxx` → `get_chat()` |
| `save_chats(chat_ids)` | READY | 见第 8 节 |
| `start_download()` | READY，且至少 1 个频道 | |
| `stop_download()` | DOWNLOADING | |
| `shutdown(timeout)` | 任意 | 关窗时调用 |

### 错误信息映射

| Pyrogram 异常 | 页面提示 | 状态 |
|---|---|---|
| `PhoneNumberInvalid` | 手机号格式不正确，请带国家区号，例如 +8613800000000 | 保持 LOGGED_OUT |
| `PhoneCodeInvalid` | 验证码不正确 | 保持 CODE_SENT |
| `PhoneCodeExpired` | 验证码已过期，请重新获取 | → LOGGED_OUT |
| `sign_in` 返回 `TermsOfService` 或 `False`（手机号未注册） | 该手机号还没有注册 Telegram，请先在手机上注册 | → LOGGED_OUT |
| `PasswordHashInvalid` | 两步验证密码不正确 | 保持 NEED_PASSWORD |
| `FloodWait(x)` | 操作太频繁，请 x 秒后再试 | 保持当前状态 |
| `ApiIdInvalid`、`ApiIdPublishedFlood` | API 凭证无效，请检查 api_id 和 api_hash | → NEED_CONFIG |
| `Unauthorized`（会话失效，如 AuthKeyUnregistered、SessionRevoked） | 登录已失效，请重新登录 | 删除本地 session → LOGGED_OUT |
| 连接超时或 `OSError` | 连接 Telegram 失败，请检查网络和代理设置 | → ERROR |
| 其他异常 | 出错了：<异常类名>，详情见日志 | → ERROR，写日志 |

## 7. 下载生命周期

### 断点续传（复用现有机制）

- `add_download_task` 入队时，会把 `node.download_status[id]` 设为 `Downloading`（`media_downloader.py:265`）。
- `update_config()` 会把所有不是 Success/Skip 的消息写进 `ids_to_retry`（`module/app.py:843-853`），并把 `last_read_message_id` 写回为已完成消息的最大 id + 1。
- 历史消息从 `last_read_message_id` 开始按 id 升序遍历（`reverse=True`，`media_downloader.py:549-554`）。

因此中途停止后，已入队但没完成的消息进入 `ids_to_retry`；还没遍历到的消息 id 都比已完成的大，下次会继续遍历到。不需要新写续传逻辑。

### `start_download(client)`（新，在 `app.loop` 中执行）

1.（由 `Controller.start_download` 在调用前执行 `app.load_config()`，重建 `chat_download_config`。CLI 的配置在启动时已经加载过，这里不再重复加载。）
2. `download_stat.reset_download_stat()`
3. 重建模块级的 `queue = asyncio.Queue()`（现在是导入时创建的，`media_downloader.py:48`），`app.is_running = True`。
4. `set_max_concurrent_transmissions(client, app.max_concurrent_transmissions)`：现在 CLI 只在创建客户端后调用一次（`media_downloader.py:665`）。GUI 的客户端是常驻的，所以每次开始下载都要重新调用，这样改过的并发数在下次"开始"时生效。它会重建 `asyncio.Semaphore`，在事件循环内调用也避免了信号量跨事件循环的问题。
5. 创建任务：`download_all_chat(client)` + `app.max_download_task` 个 `worker(client)`。任务句柄保存在模块级的 `_run_tasks` 里。
6. 返回一个 `wait_until_finished()` 协程：等价于现在的 `run_until_all_task_finish()`（所有频道 `need_check` 为真，且 `total_task == finish_task`）。它完成后，controller 在事件循环里直接调用 `media_downloader.stop_download()` 收尾（不经过 controller 的公开方法 `stop_download()`，因为后者会做状态校验），然后把状态切回 READY，附带"已完成，共下载 N 个文件"。

### `stop_download()`（新，在 `app.loop` 中执行）

1. 对每个 `chat_download_config[*].node` 调用 `stop_transmission()`，正在传输的文件会通过现有的 `StopTransmission` 路径中断（`module/download_stat.py:63-70`）。
2. `app.is_running = False`，取消 `_run_tasks` 中的任务并 `gather(..., return_exceptions=True)`。
3. `app.update_config()` 写回进度。被中断文件的临时文件留在 `temp/`，下次重新下载，保存目录里不会出现半截文件。

### CLI 的 `main()`

改为：启动客户端 → `start_download(client)` →（有 bot_token 时启动 bot，逻辑与现在相同）→ 等待完成 → 在 `finally` 里执行 `stop_download()` 和现有的收尾（停止 bot、停止客户端、打印统计）。对外行为保持不变。

### 暂停和停止

现有的"暂停/继续"按钮（全局 `DownloadState`）保持不变：暂停时协程挂起等待，客户端保持连接，可以随时继续。"停止"会结束本次下载并写回进度。两者在界面上分成两个按钮。

## 8. Web API

### token 校验

GUI 模式下注册 `before_request`：所有 `/api/*` 请求和所有 POST 请求都必须带请求头 `X-Token: <token>`，否则返回 403。页面 JS 从 URL 的 `?token=` 中读取 token，存进 `sessionStorage`，之后每个请求都带上这个请求头。带自定义请求头的跨站请求会触发 CORS 预检，服务端不返回 CORS 头，恶意网页的请求就发不出来。现有 GET 进度接口不需要 token：跨站网页能发起请求，但读不到响应内容。CLI 模式下不注册 `/api/*`，现有行为不变。

### 路由

| 方法 路径 | 请求体 | 调用 |
|---|---|---|
| `GET /api/status` | 无 | `status()` |
| `GET /api/config` | 无 | `get_config()` |
| `POST /api/config` | 见下文 | `save_config()` |
| `POST /api/retry` | 无 | `retry()` |
| `POST /api/login/phone` | `{phone}` | `send_code()` |
| `POST /api/login/code` | `{code}` | `sign_in()` |
| `POST /api/login/password` | `{password}` | `check_password()` |
| `POST /api/logout` | 无 | `log_out()` |
| `GET /api/dialogs?refresh=0|1` | 无 | `list_dialogs()` → `[{id, title, type, username}]`，只包含频道、群组、超级群组 |
| `POST /api/chats/resolve` | `{link}` | `resolve_chat()` → `{id, title, type, username}` |
| `GET /api/chats` | 无 | 当前 `config.yaml` 里的频道：`[{chat_id, dialog_id, title}]`（`chat_id` 是配置里的原值，`dialog_id` 是匹配到的对话 id，匹配不到时为 null） |
| `POST /api/chats` | `{chat_ids: [...]}` | `save_chats()` |
| `POST /api/download/start` | 无 | `start_download()` |
| `POST /api/download/stop` | 无 | `stop_download()` |

返回格式：成功 `{"ok": true, "data": ...}`；失败 `{"ok": false, "error": "<中文提示>"}`，状态码 400（参数错误）、409（当前状态不允许）或 500。

### 保存配置 `POST /api/config`

请求体：
```json
{
  "api_id": 123456,
  "api_hash": "abcdef...",
  "proxy": {"scheme": "socks5", "hostname": "127.0.0.1", "port": 7890,
            "username": "", "password": ""} ,
  "save_path": "/Users/x/Downloads/Telegram",
  "media_types": ["video", "photo"],
  "max_download_task": 5
}
```
`proxy` 为 `null` 表示不使用代理。

处理步骤：
1. 校验：
   - `api_id` 必须是正整数
   - `api_hash` 必须是 32 位十六进制
   - `proxy.scheme` 只能是 `socks5`、`http`，`port` 范围 1–65535
   - `media_types` 必须是 6 种媒体类型的非空子集
   - `max_download_task` 必须是 1–10 的整数
   - `save_path` 不存在就创建，并用 `os.access(W_OK)` 检查可写
2. 用 ruamel 往返读写（保留注释和字段顺序）：读取 `config.yaml`，**只更新这 6 个键**，然后写回。不写 `max_concurrent_transmissions`：没配置时由 `assign_config` 按 `max_download_task × 5` 自动计算（`module/app.py:506-510`），手动配置过的原样保留。proxy 为 `null` 时删除 `proxy` 键；`username`、`password` 为空字符串时不写入，否则 Pyrogram 会拿空账号去做代理认证。
3. `app.load_config()`。
4. 如果 api_id、api_hash、proxy 有改动，就重建客户端（→ CONNECTING）。

### 保存频道 `POST /api/chats`

1. 用 ruamel 读取 `config.yaml`，按提交的 `chat_ids` 顺序重建 `chat` 列表。已有的频道原样保留整个条目（包括 `last_read_message_id`、`download_filter` 等），新增的频道写成 `{chat_id: <数字 id>, last_read_message_id: 0}`。
2. 写回，然后执行 `app.load_config()` 整体重建。`update_config()` 是按下标把 `chat_download_config` 对应回 `config["chat"]` 的（`module/app.py:855-863`），整体重建才能保证下标一致。`data.yaml` 按 `chat_id` 对应，不受影响。
3. 已有频道判断是否相同时，同时比较数字 id 和用户名（旧配置里可能写的是用户名）。

## 9. 界面

在现有 `index.html`（layui）上扩展。

```
┌─ Telegram 下载器 ─────────────────────────────── ● ● ● ┐
│ ● 已登录 @alice   正在下载 3/120      [ 停止 ] [ 暂停 ] │  ← 状态栏
├────────────────────────────────────────────────────────┤
│ [设置] [账号] [频道] [下载中] [已完成]                   │
│  ……                                                    │
├────────────────────────────────────────────────────────┤
│ v2.3.0   ↓ 3.1 MB/s   浏览器访问：http://127.0.0.1:5123/?token=… [复制] │
└────────────────────────────────────────────────────────┘
```

- **状态栏**：显示状态文字和已登录的用户。主按钮在 READY 时为"开始"，在 DOWNLOADING 时为"停止"，其他状态下禁用。暂停/继续按钮只在下载中显示。每秒轮询一次 `/api/status`。
- **设置**：
  - api_id、api_hash，下面附"如何获取"的折叠说明：登录 my.telegram.org → API development tools → 创建应用 → 复制 api_id 和 api_hash
  - 代理：无 / SOCKS5 / HTTP，以及地址、端口，可选账号和密码
  - 保存目录：输入框 +[选择…]
  - 媒体类型：6 个复选框
  - 同时下载文件数：数字输入框（1–10，默认 5），旁边提示"数值越大越容易被 Telegram 限流，一般保持默认即可"
  - 下载中整个表单禁用，修改后下次点"开始"生效
- **账号**：按状态显示手机号、验证码、两步验证密码（附提示）表单；READY 时显示已登录信息和[退出登录]；ERROR 时显示原因和[重试]、[打开日志文件夹]。
- **频道**：顶部搜索框（在本地过滤），下面是带复选框的对话列表（名称、类型标签、@用户名），再往下是"粘贴链接"输入框和[添加]按钮，最底部是[保存]。已在配置中但不在对话列表里的频道也要显示出来，并默认勾选。
- **下载中 / 已完成**：保留现有的表格和接口。
- **自动跳转**：首次加载时，以及每次状态变化时，按第 6 节"页面默认标签页"切换标签页。用户手动切换过标签页之后，状态变化不再自动跳转，但进入 DOWNLOADING 时例外。

## 10. 打包与发布

### 文件

- `requirements-gui.txt`：`-r requirements.txt` + `pywebview` + `pyinstaller`（具体版本号在实现计划里验证后固定）
- `packaging/macos/tdl_gui.spec`：
  - 入口 `gui_main.py`，`console=False`，`BUNDLE(name="TelegramDownloader.app", bundle_identifier="io.github.guohai-zhang.telegram-media-downloader")`
  - `info_plist`：`LSMinimumSystemVersion=11.0`、`NSHighResolutionCapable=True`、`CFBundleShortVersionString=<utils.__version__>`
  - `datas`：只打包 `module/templates`、`module/static`，`module.parsetab` 通过 `hiddenimports` 编译进包里，**不包含 `config.yaml`、`data.yaml`、`sessions`**
- `packaging/macos/icon.icns`：v1 由脚本从一张简单的 PNG 用 `iconutil` 生成
- `packaging/macos/build.sh` + Makefile 目标 `mac-app`

### 构建步骤（`make mac-app`）

1. 检查 `uname -m` 为 `arm64`。
2. 用 `uv venv --seed --python 3.11 --python-preference only-managed build/venv-gui` 创建隔离环境（uv 管理的 CPython 的 minos 是 11.0，Homebrew 的是 13.0），安装 `requirements-gui.txt`。
3. 设 `export MACOSX_DEPLOYMENT_TARGET=11.0`（对源码编译的 C 扩展生效）。
4. 运行 `python gen_filter_cache.py`，生成 ply 的 `parsetab.py` 和 `parser.out`。
5. `pyinstaller packaging/macos/tdl_gui.spec --noconfirm`。
6. ad-hoc 签名：`codesign --force --deep -s - dist/TelegramDownloader.app`，然后 `codesign --verify --deep --strict`。完全没签名的 arm64 应用，从网上下载后系统会提示"已损坏"，而且没有"仍要打开"的选项。
7. 检查最低系统版本：`packaging/macos/check_minos.py`，对 app 里所有 Mach-O 文件执行 `otool -l`，确认 `minos` ≤ 11.0。有超出的就列出来并让构建失败。
8. 打包 `.dmg`：临时目录里放 `.app` 和一个指向 `/Applications` 的软链接，然后 `hdiutil create -format UDZO`。输出为 `dist/TelegramDownloader-<version>-arm64.dmg`。

### 版本号

本功能发布为 `2.3.0`，修改 `utils/__init__.py` 中的 `__version__`。

### 发布到 GitHub

- `.dmg` **不提交进 git**（`dist/` 已在 `.gitignore` 中），上传到 `github.com/guohai-Zhang/telegram_media_downloader` 的 Releases，tag 为 `v2.3.0`。
- 上传方式：在 GitHub 网页的 Releases 页面手动上传，或者 `brew install gh` 后执行 `gh release create v2.3.0 dist/*.dmg`。
- 推送代码和创建 Release 属于对外操作，执行前要先经过仓库所有者确认。
- Release 说明里写明：只支持 Apple Silicon，要求 macOS 11 及以上，并附首次打开的步骤。

### README（中英文各加一节"Mac 图形版"）

- 下载：Releases 页面 → `.dmg` → 拖进"应用程序"
- 首次打开：
  - macOS 14 及以下：在"应用程序"里右键点击 → 打开 → 打开
  - macOS 15 及以上：双击后被拦截 → 系统设置 → 隐私与安全性 → 往下找到"仍要打开" → 输入密码
  - 兜底：终端执行 `xattr -dr com.apple.quarantine /Applications/TelegramDownloader.app`
- 如何获取 api_id 和 api_hash
- 数据位置：`~/Library/Application Support/TelegramMediaDownloader/`（配置、登录状态、日志）

## 11. 错误处理汇总

| 情况 | 处理 |
|---|---|
| 连接失败 / 代理错误 | 超时 20s → ERROR，提示检查网络和代理，提供[重试] |
| 登录各类错误 | 按第 6 节映射为中文提示 |
| 保存目录不可写 | 拒绝保存，提示原因 |
| 配置文件损坏（YAML 解析失败） | 启动时把它备份为 `config.yaml.broken-<时间戳>`，写入默认配置，并在页面上提示一次 |
| controller 未预期的异常 | → ERROR，写日志，页面提供[打开日志文件夹] |
| 第二个实例 | 文件锁冲突 → 弹出提示后退出 |
| 下载中关窗 | 弹原生确认框；确认后 `shutdown(10)`，保存进度 |
| `shutdown` 超时 | 记录警告，强制退出。进度可能丢失最后几秒，下次最多重复下载几个文件 |
| 单个文件下载失败 | 沿用现有的重试和 `ids_to_retry` 逻辑 |

## 12. 测试

### 单元测试（pytest，放在 `tests/` 下，CI 在 Ubuntu 和 Windows 上跑 Python 3.8–3.12）

- `tests/module/test_controller.py`：用假客户端（`AsyncMock`）覆盖第 6 节的所有状态转换和错误映射，包括重新发送验证码、两步验证、FloodWait，以及修改凭证后重建客户端、`InvalidState` 拒绝操作。
- `tests/module/test_web_api.py`（Flask test client）：
  - 没有 token 或 token 错误返回 403
  - 各路由的参数校验，409 和 400
  - 保存配置后，高级项和注释不丢失（用一份带注释、带过滤器的 `config.yaml` 做往返测试）
  - `max_download_task` 超出 1–10 返回 400；手动配置的 `max_concurrent_transmissions` 保存后保持不变，没配置时不会被写入
  - 保存频道：保留旧条目、新增条目默认值、顺序正确、按用户名匹配
- `tests/test_media_downloader.py`：现有调用 `main()` 的 3 个用例保持通过。新增 `start_download` / `stop_download` 的测试：停止后 `ids_to_retry` 包含未完成的消息，`queue` 每次重建，worker 数量等于 `max_download_task`，每次开始都会重新设置客户端的信号量。
- `tests/module/test_download_stat.py`：`reset()`。
- `tests/test_gui_bootstrap.py`：数据目录创建、默认配置生成、损坏配置的备份、端口被占用时换端口（先占住一个端口再启动）、单实例锁（在 Windows 上跳过）。这些测试不导入 `webview`，只测 `gui_main` 中拆出来的纯函数。

### 手工端到端清单（真实账号）

1. 在全新的 macOS 用户账号中，打开从 Releases 下载的 `.dmg`（带 quarantine 标记），按 README 的步骤放行并打开。
2. 填写配置 → 验证码登录；另用一个开启了两步验证的账号测一次。
3. 勾选 1 个频道 + 粘贴 1 个公开频道链接 → 保存 → 开始 → 下载中列表有进度和速度。
4. 暂停 → 继续；停止 → 再开始，确认从中断处继续，没有重复下载已完成的文件。
5. 下载中关窗 → 确认框 → 重新打开：自动登录，进度延续。
6. 双击第二次：提示已在运行。
7. 复制浏览器访问地址，用 Safari 打开，页面功能正常。
8. 命令行 `python media_downloader.py` 用一份旧的 config.yaml 跑一遍，行为和改动前一致。

## 13. 风险与待验证

| 风险 | 验证方式 / 应对 |
|---|---|
| `requirements.txt` 固定的老版本（如 `PyYAML==5.3.1`、`PyTgCrypto==1.2.6`）在 Python 3.11 arm64 上没有 wheel，从源码编译失败 | 实现计划的第一个任务先在隔离环境里安装验证。失败时，只在 `requirements-gui.txt` 里覆盖为能用的版本，不改 CLI 的固定版本 |
| pywebview 或 pyobjc 的最新版要求的系统版本高于 11 | 构建时的第 7 步 `otool` 检查。必要时固定到较老的版本，或者把最低版本提高到能满足的系统（并更新 README） |
| PyInstaller 漏掉 pyrogram 或 pywebview 的隐式导入 | spec 里补 `hiddenimports`。手工清单第 1 步能发现这类问题 |
| 各版本 macOS 上 Gatekeeper 的提示文字和放行路径不同 | README 分版本写，并提供 `xattr` 兜底命令 |
| 在同一个进程里反复开始和停止，全局状态有残留 | `start_download` 显式重置，并加单元测试覆盖。手工清单第 4 步 |
