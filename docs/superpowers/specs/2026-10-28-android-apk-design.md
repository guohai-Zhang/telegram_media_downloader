# Android APK 版 Telegram Media Downloader — 设计文档

日期：2026-10-28
状态：待实现

## 1. 意图与动机

把现有的 macOS 桌面应用做成一个可以装在 Android 手机上的 APK，功能与桌面版一比一对齐。

驱动场景（用户原话）：**"刷到一个频道想立刻存下来"**。人不在电脑前的时候，希望掏出手机就能当场建任务、当场开始下载，而不是记着回到电脑前再处理。

因此这不是桌面版的遥控器，而是一套**独立完整、自给自足**的实现：手机上自己登录 Telegram、自己下载、自己备份，不依赖电脑开机，也不需要和电脑同步状态。

### 为什么现在可行

关键前提是现有架构已经为此铺好了路：macOS GUI（`gui_main.py`）本质上是 **一个本地 Flask 服务 + 一个 pywebview 窗口壳**。界面是 `module/templates/` 下的 HTML，由 Flask 渲染。

这意味着移植 Android 时，**界面层完全不需要重写**——把 pywebview 窗口换成 Android WebView，指向同一个本地 Flask 服务即可。要替换的只是那层很薄的原生外壳。

### 探路验证结论（已完成）

动工前已用一次性 spike 工程在 arm64 模拟器上实测，结果全部通过：

```
python 3.11.6 / aarch64
OK   tgcrypto     -> ige256 roundtrip ok
OK   pycryptodome -> AES block ok
OK   flask        -> 2.2.2
OK   pyrogram     -> 2.0.106
OK   sqlite3      -> 3.39.2
```

最大的未知风险（TgCrypto 这个 C 扩展是否需要自行交叉编译）**已排除**：Chaquopy 官方仓库提供 cp311 arm64-v8a 预编译 wheel，AES-IGE 加解密往返验证通过。

**此项目已无未知的技术死胡同，剩余的都是工作量。**

## 2. 目标

1. 产出一个可侧载安装的 Android APK，功能与桌面版一致——包括 per-chat 文件大小限制、起始月份、skipped 统计、uploading 进度、备份到 Saved Messages。
2. 界面与桌面版**完全一致**（复用同一套 Flask 模板，不是仿制）。
3. 切换到其他 app 或锁屏后，下载任务**继续运行**。
4. 下载的文件留在手机本地，且**系统相册/播放器能直接扫到并播放**。
5. 登录流程与桌面版一致：手机号 + 验证码，在 app 内完成。
6. Python 业务逻辑（`module/`、`media_downloader.py`）尽量保持单一代码库，与桌面版共用。

## 3. 非目标

- 上架 Google Play 或任何应用商店（仅侧载自用）。
- 与桌面版之间同步配置、session 或下载进度。
- 支持 armeabi-v7a（32 位）等老旧 ABI。
- 多用户或账号隔离。
- 替代或改动桌面版的现有行为。

## 4. 反目标（即使技术上跑通了，出现以下情况也算失败）

- **界面需要单独维护一套**。一旦 Android 的页面和桌面版的页面分叉成两份模板，这个项目就失败了——维护成本会迅速吃掉它带来的价值。
- **锁屏几分钟后任务被静默杀死**，且用户无从知晓。宁可明确提示"系统限制了后台"，也不能假装还在下。
- **文件下载成功但在相册里看不见**，用户得靠文件管理器翻目录才能找到。
- **为了跑在 Android 上，把 `module/` 里的业务逻辑改得面目全非**，导致桌面版回归。平台差异必须收敛在适配层，不能扩散进业务代码。

## 5. 约束

### 既有技术栈（必须沿用）

- Python 业务逻辑：`module/`（约 7000 行）、`media_downloader.py`（976 行）
- Web 框架：Flask 2.2.2 + flask-login
- 界面模板：`module/templates/`（`index.html` 388 行、`gui_panels.html` 133 行、`login.html` 68 行）
- Telegram 客户端：fork 版 pyrogram（`tangyoha/pyrogram` 的 `patch` 分支）
- Web 服务入口：`module/web.py` 的 `make_web_server()` / `register_gui()`

### 目标设备

- 一加手机（OxygenOS / ColorOS 系）
- arm64-v8a，Android 10 及以上（minSdk 24，targetSdk 34）

### 依赖版本约束（探路实测得出，不可随意改动）

| 依赖 | 桌面版 | Android 上必须 | 原因 |
|---|---|---|---|
| `pycryptodome` | 3.18.0 | **3.21.0** | Chaquopy 仅预编译此版本 |
| TgCrypto | `PyTgCrypto==1.2.6` | **`TgCrypto==1.2.5`** | 包名与版本均不同，Chaquopy 仓库中的是 `TgCrypto` |
| `Werkzeug` | 2.2.2 | **2.2.2（必须锁死）** | 漂到 2.3+ 会因 `url_quote` 移除导致 flask 直接 ImportError |
| Python | — | **3.11** | 与已验证的 wheel ABI 对齐 |

### 平台耦合点（现有代码中需要适配的位置）

经排查，macOS 耦合**高度集中在 `gui_main.py`**，业务逻辑层基本干净：

| 位置 | 内容 | Android 处理 |
|---|---|---|
| `gui_main.py:44` | `~/Library/Application Support` 路径 | 换成 Android app 私有目录 |
| `gui_main.py:54,60` | `fcntl.flock` 单实例锁 | Android 由系统保证单实例，可去除 |
| `gui_main.py:118` | `osascript` 弹窗 | 换成原生 Android 对话框 |
| `gui_main.py:98-100` | pywebview 文件夹选择对话框 | 换成 Android SAF 目录选择器 |
| `module/app.py:422-423` | `save_path` / `temp_save_path` 默认取 `os.path.abspath(".")` | 由原生层注入 Android 路径 |

## 6. 架构

三层结构，各层职责严格分离：

```
┌─────────────────────────────────────────────┐
│  界面层（零改动）                              │
│  module/templates/*.html                     │
└─────────────────────────────────────────────┘
                   ↑ HTTP
┌─────────────────────────────────────────────┐
│  Python 层（原样搬运 + 薄适配层）               │
│  module/ + media_downloader.py               │
│  新增 android_platform.py 吸收平台差异         │
└─────────────────────────────────────────────┘
                   ↑ Chaquopy JNI
┌─────────────────────────────────────────────┐
│  原生层（新写）                                │
│  前台服务 + WebView 壳 + 存储桥接 + 权限引导     │
└─────────────────────────────────────────────┘
```

### 6.1 打包工具链：Chaquopy（已决定）

**选择 Chaquopy，而非 buildozer / python-for-android。**

理由：本项目必须编写真实的原生 Android 组件——前台服务（后台下载）、MediaStore 写入（相册可见）、SAF 目录选择、厂商电源管理引导。Chaquopy 的模型是"在一个正常的 Android Studio 工程里嵌入 Python"，原生那一半可以用标准 Kotlin/Java 自由编写。buildozer 是为"纯 Python 应用"设计的，一旦需要深度触碰原生 API 就处处受限。

工具链已在本机验证可用：Gradle 8.10.2、AGP 8.6.1、Chaquopy 15.0.1、JDK 17、Android SDK/NDK 齐备。

### 6.2 原生层

**前台服务（Foreground Service）**
- 类型 `dataSync`，托管 Flask 服务与下载循环的整个生命周期
- 常驻通知显示当前进度（下载中的文件名 / 总进度）
- 这是 Android 唯一允许长时间后台作业的正规途径，直接对应目标 3

**WebView 壳**
- 单 Activity，内嵌 WebView 指向 `http://127.0.0.1:<port>`
- 端口由 `make_web_server(host, 0)` 动态分配（`module/web.py:131` 已支持），经 JNI 回传给原生层
- 需启用 JavaScript 与 DOM storage，与 pywebview 的行为对齐
- **Activity 销毁不得终止服务**——退出界面后下载继续

**存储桥接（关键决策）**

申请 `MANAGE_EXTERNAL_STORAGE`（"所有文件访问权限"），在共享存储上保留一个**真实可读写的普通目录**作为 `save_path`。

这个决定是整个方案的支点，理由：大量现有业务逻辑是直接按路径扫目录的，而非只写不读——

- `module/saved_backup.py:53,57,81,90,339,385`：`os.path.isdir` / `os.listdir` / `os.path.getsize`，然后打开文件上传到 Saved Messages
- `module/media_index.py:108-114`：按路径扫描 + `getsize`，支撑去重与 skipped 统计
- `module/app.py:798-820` `get_file_save_path`：构建 `chat_title/media_datetime` 嵌套子目录
- `media_downloader.py:399-402`：`os.path.relpath(file_name, app.save_path)`

MediaStore 没有等价的「app 自定义嵌套目录树」语义，若以 MediaStore 为准，上述全部逻辑都要改成无路径写法——这正面违反反目标 4。而 `MANAGE_EXTERNAL_STORAGE` 能让这些代码**一行不改**地继续工作，相册也能扫到。

代价是安装后需手动授予一个措辞吓人的权限，且无法上架——但本项目本就只侧载自用（见非目标 1），这个代价不成立为阻碍。

- 临时文件（`temp_save_path`）仍留在 app 私有目录，避免半成品出现在相册
- 下载完成后 `shutil.move` 到共享目录，再触发媒体库扫描使其立即可见

**厂商电源管理引导**
- 一加属于后台管理激进的厂商，即使前台服务也可能被杀
- 首次启动引导页，直接跳转到"电池 → 不受限制"与"允许自启动"设置项
- 这是**代码无法解决、只能引导用户**的部分，必须显式告知而非静默失败

### 6.3 Python 适配层

新增单一适配模块（建议 `module/android_platform.py`），集中吸收全部平台差异。**业务代码不得散落 `if android` 判断**（见反目标）。

该层负责：
- 由原生层注入数据目录、下载目录、临时目录
- 替换 `gui_main.py` 中的 macOS 专有逻辑（路径、单实例锁、弹窗）
- 维护下载状态日志，支撑被杀后的中断提示与断点续传（见 6.5）
- 下载完成后回调原生层，触发 MediaStore 发布

`gui_main.py` 本身不移植，而是写一个等价的 Android 入口，复用其编排顺序——注意其文件头注释强调的约束：**工作目录必须在 `media_downloader` 导入之前指向数据目录**，因为 `Application()` 在构造时就从当前目录解析 config / session / log / temp 路径。此约束在 Android 上同样成立，且因 Chaquopy 的启动时序更易踩坑，须优先处理。

### 6.4 被系统杀死后的行为

一加的 OxygenOS / ColorOS 即使对前台服务也可能强杀，这是代码无法根除的。要求是：**绝不静默死亡**（反目标 2）。

- 持久化下载状态日志，记录当前任务与正在下载的文件进度
- 下次启动时检测到非正常退出，在界面上明确提示「上次下载被系统中断」
- 未下完的文件**从断点继续**，而非从头重下

注意现状：`media_downloader.py:112` 目前只在文件完整下载后做 `shutil.move`，没有断点续传机制。因此这部分需要新增——这是本项目工作量最大的单项，实现者应单独评估。

### 6.5 功能范围裁剪

以下两个模块**不打进 APK**：

- `module/cloud_drive.py`——`cloud_drive.py:125` 使用 `create_subprocess_shell`，Chaquopy 下无法运行，技术上不可行
- `module/bot.py`（1479 行）——Telegram 机器人控制。目标场景（刷到频道就存、备份到 Saved Messages）不需要它；且 bot 需常驻监听，会与前台服务的生命周期纠缠，增加后台被杀的排查难度

实现时需确认二者的排除不会破坏 `module/` 内的导入链。

### 6.6 登录流程

与桌面版完全一致：沿用现有 Flask 登录页（`module/templates/login.html`）与 flask-login 机制，手机号 + 验证码在 WebView 内完成。session 文件存于 app 私有目录。不从桌面版导入已有 session。

## 7. 其余已定事项（作者判断，非用户决定）

以下由设计者直接裁定，实现者如有更好方案可提出，但需说明理由：

**下载目录选择器**——移除。`gui_main.py:98-100` 的 pywebview 文件夹对话框不迁移。SAF 返回 `content://` tree URI，Python 的 `os.*` 无法使用，桥接需要为每次读写和扫描套一层 DocumentFile shim，成本极高。既然已采用 `MANAGE_EXTERNAL_STORAGE`，保存路径由原生层注入一个固定的共享目录即可。`gui_panels.html:43-44` 的 `save_path` 文本框在 Android 下置为只读展示。

**界面不分叉的界线**——不新增第二套模板文件。模板已有 native 模式标志（`index.html:20` 的 `data-native`、`web.py:231` 的 `native_mode`），允许在共享模板内扩展该标志以区分 Android。但**电源管理引导、权限申请这类纯 Android 概念一律放在原生 Activity/对话框中**，不进 WebView，避免桌面版视图里出现无意义分支。

**依赖版本**——新增独立的 `requirements-android.txt`，不改动桌面版 `requirements.txt` 的既有 pin。理由：`PyYAML==5.3.1` 在现代工具链上有已知的 sdist 构建失败问题，大概率需要在 Android 侧上浮版本；强行让两端收敛到同一组 pin 会把 Android 的约束倒灌进桌面版，徒增回归风险。代价是两端存在版本漂移，需在该文件中注明每处偏离的原因。

**本地服务鉴权**——沿用现有的 loopback + `X-Token` 机制（`web.py:119,136-157`），不另起炉灶。需在实现中确认 token 为每次启动随机生成（`gui_main.py:9` 已引入 `secrets`）。注意这里有个已知的模型弱点：Android 上 `127.0.0.1:<port>` 对本机其他 app 可达，而 `web.py` 中基于 Host 头的防护本质是浏览器同源论证，对原生 app 的裸 HTTP 请求不成立。鉴于设备为个人自用且非 root，暂接受该风险，但应在代码中注释标明，不要让后人误以为这里是安全的。

**pyrogram fork 的安装方式**——若 Chaquopy 的 pip 无法从 GitHub zip URL 安装，则将 `tangyoha/pyrogram` 的 `patch` 分支 vendor 进仓库作为源码目录加入 Python path。相比发布私有 wheel 或把 fork 的改动重新打到上游 2.0.106 上，vendor 最直白、最容易回溯。

## 8. 验收标准

- 功能验收在 arm64 模拟器上完成，须覆盖：登录、建任务、大小限制、起始月份、skipped 统计、备份到 Saved Messages、切后台与锁屏后下载继续
- 产出 APK 交由用户在自己的一加设备上侧载试用。**厂商电源管理相关的行为只能在真机验证**，不作为模拟器验收项
- Android 侧不要求接入现有 pytest / CI；Python 业务逻辑若有改动，仍须保证原有测试不回归

## 9. 留给实现者的决定

- 原生层用 Kotlin 还是 Java（建议 Kotlin，但不强制）
- 前台服务与 Flask 线程的具体生命周期编排方式
- 通知栏进度的更新频率与节流策略
- 下载完成后触发媒体库扫描的具体时机与失败重试策略
- 断点续传的状态日志格式与落盘频率（见 6.4）
- 适配层与原生层之间的 JNI 接口形态
- 单仓库共用代码的组织方式（Android 工程与现有 Python 代码的目录关系）

## 10. 开放问题

- fork 版 pyrogram（`tangyoha/pyrogram` 的 `patch` 分支）尚未在 Android 上实测。探路时用的是上游 `pyrogram==2.0.106`。因 fork 为纯 Python、无编译环节，风险评估为低，但实现时应尽早验证。
- Chaquopy 的 pip 如何安装 fork 版 pyrogram（从 GitHub zip URL 安装）需要验证；失败则按 §7 vendor 进仓库。
- `requirements.txt` 中除探路已验证的 5 个之外（`PyYAML==5.3.1`、`ruamel.yaml`、`loguru`、`rich`、`ply`、`requests`、`flask-login`）尚未在 Chaquopy 上实测。其中 `PyYAML==5.3.1` 风险最高（已知 sdist 构建问题），应优先验证。

---

## 附：验证过的构建配置（来自探路工程）

```gradle
android {
    compileSdk 34
    defaultConfig {
        minSdk 24
        targetSdk 34
        ndk { abiFilters "arm64-v8a" }
        python {
            version "3.11"
            pip {
                install "TgCrypto==1.2.5"
                install "pycryptodome==3.21.0"
                install "Werkzeug==2.2.2"
                install "flask==2.2.2"
                // pyrogram: 需改为 fork 版
            }
        }
    }
}
```
