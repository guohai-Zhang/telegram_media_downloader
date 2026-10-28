# Android APK 版 Telegram Media Downloader 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把现有 macOS 桌面应用做成功能一比一的 Android APK，支持后台下载、断点续传、文件落入相册可见。

**Architecture:** 三层。界面层复用 `module/templates/` 不动；Python 层原样搬运 `module/` 与 `media_downloader.py`，新增一个平台适配模块吸收全部差异；原生层新写前台服务、WebView 壳与存储桥接。Python 侧的改动（断点续传、中断状态、平台适配）全部先在桌面用 pytest 验证通过，再进 Android 工程。

**Tech Stack:** Chaquopy 15.0.1 + AGP 8.6.1 + Gradle 8.10.2 + JDK 17 + Python 3.11；Kotlin 原生层；现有 Flask 2.2.2 + fork 版 pyrogram。

**Spec:** `docs/superpowers/specs/2026-10-28-android-apk-design.md`

## Global Constraints

- Python 版本固定 **3.11**，ABI 仅 **arm64-v8a**，minSdk **24**，targetSdk **34**。
- 依赖版本（Android 侧，不可随意改动）：`TgCrypto==1.2.5`、`pycryptodome==3.21.0`、`Werkzeug==2.2.2`（锁死，漂到 2.3+ 会因 `url_quote` 移除导致 flask ImportError）、`flask==2.2.2`。
- Android 依赖写入独立的 `requirements-android.txt`，**不改动桌面版 `requirements.txt` 的既有 pin**。每处与桌面版的偏离都要在该文件中注明原因。
- 平台差异一律收敛在适配层，**业务代码中不得出现 `if android` 分支**（设计文档反目标 4）。
- 不新增第二套 HTML 模板。Android 专有的权限申请、电源管理引导放在原生层，不进 WebView。
- `module/bot.py` 与 `module/cloud_drive.py` 不打进 APK。
- 工作目录必须在 `media_downloader` 导入**之前**指向数据目录——`Application()` 在构造时就从当前目录解析 config/session/log/temp 路径。
- 桌面版行为不得回归：现有 pytest 套件必须全程保持通过。

## Review Focus

这些是设计文档隐含、但不属于任何任务主线的失败模式。每条都已在下方对应任务中补了测试。

1. **断点续传时 temp 文件比服务端文件还大**（手工改过、上次写坏）——应丢弃 temp 重下，而不是从超出的 offset 继续拿到损坏文件。→ Task 2
2. **断点续传中途文件引用过期**（`FileReferenceExpired`）——已下载的部分应保留，刷新 message 后从原 offset 继续，而非从头再来。→ Task 2
3. **状态日志自身损坏或为空**（被杀在写入途中）——启动时应当作"无中断记录"正常启动，而不是崩在解析上。→ Task 3
4. **共享存储权限被用户拒绝**——应给出明确提示并停在引导页，而不是让下载静默失败或崩溃。→ Task 8
5. **同一文件名在相册中重复注册**——重复下载或续传完成后重复扫描，不应在相册里留下多条同路径记录。→ Task 9

---

## File Structure

**Python 侧（先行，桌面可测）**
- `module/platform_paths.py`（新建）——平台路径解析，桌面与 Android 共用一个接口
- `module/resume.py`（新建）——断点续传的 offset 计算与 temp 文件校验
- `module/run_journal.py`（新建）——下载状态日志，支撑被杀后的中断提示
- `media_downloader.py:578-600`（修改）——下载调用点改为可续传
- `android_main.py`（新建）——Android 入口，对应 `gui_main.py` 的编排顺序

**Android 工程（后行）**
- `android/app/build.gradle`——Chaquopy 配置与依赖
- `android/app/src/main/java/.../MainActivity.kt`——WebView 壳
- `android/app/src/main/java/.../DownloadService.kt`——前台服务与通知
- `android/app/src/main/java/.../StorageBridge.kt`——权限、路径注入、媒体库扫描
- `android/app/src/main/java/.../OnboardingActivity.kt`——权限与电源管理引导

---

## Task 1: 平台路径解析

把 `gui_main.py` 中硬编码的 macOS 路径抽成一个两端共用的接口，让 Android 原生层能注入自己的目录。

**Files:**
- Create: `module/platform_paths.py`
- Modify: `gui_main.py:37-49`（改为调用新模块）
- Test: `tests/module/test_platform_paths.py`

**Interfaces:**
- Produces:
  - `resolve(home: Optional[str] = None, overrides: Optional[Dict[str, str]] = None) -> Paths`
  - `Paths` 为 dataclass，字段：`data_dir: str`、`save_path: str`、`temp_dir: str`、`log_dir: str`
  - `overrides` 非空时逐字段覆盖，供 Android 原生层注入；为空时走桌面默认值

- [ ] **Step 1: 写失败测试**

```python
def test_desktop_defaults_match_existing_gui_main(self):
    paths = platform_paths.resolve(home=self.tmp)
    self.assertEqual(
        paths.data_dir,
        os.path.join(self.tmp, "Library", "Application Support", "TelegramMediaDownloader"),
    )
    self.assertEqual(paths.save_path, os.path.join(self.tmp, "Downloads", "Telegram"))

def test_overrides_win_over_defaults(self):
    paths = platform_paths.resolve(home=self.tmp, overrides={"save_path": "/sdcard/TG"})
    self.assertEqual(paths.save_path, "/sdcard/TG")
    self.assertEqual(
        paths.data_dir,
        os.path.join(self.tmp, "Library", "Application Support", "TelegramMediaDownloader"),
    )

def test_override_of_unknown_key_raises(self):
    with self.assertRaises(KeyError):
        platform_paths.resolve(home=self.tmp, overrides={"nope": "/x"})
```

- [ ] **Step 2: 跑测试确认失败**

Run: `py.test tests/module/test_platform_paths.py -v`
Expected: FAIL，`ModuleNotFoundError: module.platform_paths`

- [ ] **Step 3: 实现 `module/platform_paths.py`**

桌面默认值逐字照搬 `gui_main.py:42-49` 的现有行为（`APP_DIR_NAME = "TelegramMediaDownloader"`），`TDL_GUI_HOME` 环境变量的语义保持不变。未知 override 键抛 `KeyError`，避免原生层拼错字段名后静默失效。

- [ ] **Step 4: 跑测试确认通过**

Run: `py.test tests/module/test_platform_paths.py -v`
Expected: PASS

- [ ] **Step 5: 改 `gui_main.py` 调用新模块，确认桌面无回归**

`data_dir()` 与 `default_save_path()` 改为转调 `platform_paths.resolve()`，保留原函数名与签名（`tests/test_gui_bootstrap.py` 依赖它们）。

Run: `py.test tests/test_gui_bootstrap.py tests/module/test_platform_paths.py -v`
Expected: PASS

- [ ] **Step 6: 提交**

```bash
git add module/platform_paths.py tests/module/test_platform_paths.py gui_main.py
git commit -m "feat: 抽出平台路径解析，为 Android 注入目录做准备"
```

---

## Task 2: 断点续传

当前 `media_downloader.py:582` 调用 `client.download_media()`，它不支持从中途继续；进程被杀后 temp 文件作废。改为基于 `stream_media(offset=...)` 的分块写入。

**Files:**
- Create: `module/resume.py`
- Modify: `media_downloader.py:578-600`
- Test: `tests/module/test_resume.py`

**Interfaces:**
- Consumes: 无
- Produces:
  - `plan_resume(temp_path: str, media_size: int, chunk_size: int) -> ResumePlan`
  - `ResumePlan` 为 dataclass，字段：`start_offset: int`、`discard_temp: bool`
  - 语义：temp 不存在 → `(0, False)`；temp 小于 `media_size` → 从对齐到 `chunk_size` 的边界继续；temp 大于等于 `media_size` → `(0, True)`，调用方须先删除 temp 再从头下

- [ ] **Step 1: 写失败测试**

```python
CHUNK = 1024 * 1024

def test_no_temp_file_starts_from_zero(self):
    plan = resume.plan_resume(os.path.join(self.tmp, "missing"), 10 * CHUNK, CHUNK)
    self.assertEqual((plan.start_offset, plan.discard_temp), (0, False))

def test_partial_temp_resumes_on_chunk_boundary(self):
    path = self._write(b"x" * (3 * CHUNK + 17))
    plan = resume.plan_resume(path, 10 * CHUNK, CHUNK)
    self.assertEqual((plan.start_offset, plan.discard_temp), (3 * CHUNK, False))

def test_temp_larger_than_media_is_discarded(self):
    path = self._write(b"x" * (11 * CHUNK))
    plan = resume.plan_resume(path, 10 * CHUNK, CHUNK)
    self.assertEqual((plan.start_offset, plan.discard_temp), (0, True))

def test_temp_exactly_media_size_is_discarded(self):
    path = self._write(b"x" * (10 * CHUNK))
    plan = resume.plan_resume(path, 10 * CHUNK, CHUNK)
    self.assertEqual((plan.start_offset, plan.discard_temp), (0, True))

def test_empty_temp_starts_from_zero(self):
    path = self._write(b"")
    plan = resume.plan_resume(path, 10 * CHUNK, CHUNK)
    self.assertEqual((plan.start_offset, plan.discard_temp), (0, False))
```

第 3、4 条对应 Review Focus 1：temp 比服务端文件大或相等时，说明这个 temp 不可信（手工改过，或上次写坏），必须丢弃重下而非从超出的 offset 继续。

- [ ] **Step 2: 跑测试确认失败**

Run: `py.test tests/module/test_resume.py -v`
Expected: FAIL，`ModuleNotFoundError: module.resume`

- [ ] **Step 3: 实现 `module/resume.py`**

`start_offset` 向下对齐到 `chunk_size` 的整数倍，因为最后一个不完整的块无法确认是否写全。

- [ ] **Step 4: 跑测试确认通过**

Run: `py.test tests/module/test_resume.py -v`
Expected: PASS

- [ ] **Step 5: 写下载调用点的失败测试**

在 `tests/test_media_downloader.py` 中新增，用 mock client 验证两件事：

```python
def test_resumes_from_existing_temp_offset(self):
    # temp 已有 3 个 chunk，stream_media 应以 offset=3*CHUNK 被调用
    ...
    self.assertEqual(client.stream_media.call_args.kwargs["offset"], 3 * CHUNK)

def test_file_reference_expired_keeps_partial_and_resumes(self):
    # 首次 stream 抛 BadRequest，刷新 message 后重试
    # 断言：temp 文件未被删除，第二次 stream_media 的 offset 等于中断时已写入的字节数
    ...
```

第二条对应 Review Focus 2：引用过期是常见情况（`media_downloader.py:601` 已有处理分支），不能因此丢掉已下好的部分。

- [ ] **Step 6: 跑测试确认失败**

Run: `py.test tests/test_media_downloader.py -k resume -v`
Expected: FAIL

- [ ] **Step 7: 改写 `media_downloader.py:578-600` 的下载循环**

用 `client.stream_media(message, offset=plan.start_offset)` 逐块写入 temp 文件（以 `"ab"` 追加打开，`discard_temp` 为真时先删除），保持现有的 `update_download_status` 进度回调语义与三次重试结构不变。下载完成后仍走 `_check_download_finish` 与 `_move_to_download_path`（`media_downloader.py:597-598`）。

- [ ] **Step 8: 跑全套测试确认无回归**

Run: `py.test tests/ -v`
Expected: PASS

- [ ] **Step 9: 提交**

```bash
git add module/resume.py tests/module/test_resume.py media_downloader.py tests/test_media_downloader.py
git commit -m "feat: 下载支持断点续传"
```

---

## Task 3: 中断状态日志

被系统杀死后，下次启动要能说出"上次被中断"。进程无法在被杀时通知任何人，所以靠持久化状态 + 启动时检测。

**Files:**
- Create: `module/run_journal.py`
- Test: `tests/module/test_run_journal.py`

**Interfaces:**
- Consumes: 无
- Produces:
  - `RunJournal(path: str)`
  - `.mark_running(chat_id: int, message_id: int, file_name: str) -> None`
  - `.mark_clean_exit() -> None`
  - `.last_interrupted() -> Optional[InterruptedRun]`——返回上次非正常退出时正在下载的内容，正常退出或无记录则返回 `None`
  - `InterruptedRun` 为 dataclass，字段：`chat_id: int`、`message_id: int`、`file_name: str`

- [ ] **Step 1: 写失败测试**

```python
def test_clean_exit_reports_no_interruption(self):
    j = run_journal.RunJournal(self.path)
    j.mark_running(1, 2, "a.mp4")
    j.mark_clean_exit()
    self.assertIsNone(run_journal.RunJournal(self.path).last_interrupted())

def test_missing_clean_exit_reports_interruption(self):
    j = run_journal.RunJournal(self.path)
    j.mark_running(42, 7, "big.mp4")
    # 不调用 mark_clean_exit，模拟被杀
    found = run_journal.RunJournal(self.path).last_interrupted()
    self.assertEqual((found.chat_id, found.message_id, found.file_name), (42, 7, "big.mp4"))

def test_no_journal_file_reports_no_interruption(self):
    self.assertIsNone(run_journal.RunJournal(self.path).last_interrupted())

def test_corrupt_journal_reports_no_interruption(self):
    with open(self.path, "w", encoding="utf-8") as f:
        f.write("{not json")
    self.assertIsNone(run_journal.RunJournal(self.path).last_interrupted())

def test_empty_journal_reports_no_interruption(self):
    open(self.path, "w", encoding="utf-8").close()
    self.assertIsNone(run_journal.RunJournal(self.path).last_interrupted())
```

后两条对应 Review Focus 3：日志自身可能被杀在写入途中，解析失败必须当作"无中断记录"正常启动，绝不能让一个损坏的日志把 app 卡在启动阶段。

- [ ] **Step 2: 跑测试确认失败**

Run: `py.test tests/module/test_run_journal.py -v`
Expected: FAIL，`ModuleNotFoundError: module.run_journal`

- [ ] **Step 3: 实现 `module/run_journal.py`**

JSON 单文件。`mark_running` 每次覆盖写入，落盘用临时文件 + `os.replace` 保证原子性。读取时任何异常一律返回 `None`。

- [ ] **Step 4: 跑测试确认通过**

Run: `py.test tests/module/test_run_journal.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add module/run_journal.py tests/module/test_run_journal.py
git commit -m "feat: 新增下载状态日志，支撑被杀后的中断检测"
```

---

## Task 4: Android 入口与依赖清单

对应 `gui_main.py` 的编排，但去掉 macOS 专有部分（`fcntl` 锁、`osascript` 弹窗、pywebview 窗口与文件夹选择器）。

**Files:**
- Create: `android_main.py`
- Create: `requirements-android.txt`
- Test: `tests/test_android_bootstrap.py`

**Interfaces:**
- Consumes: `platform_paths.resolve`（Task 1）、`RunJournal`（Task 3）
- Produces:
  - `start(overrides: Dict[str, str]) -> int`——启动 Flask 与下载控制器，返回实际监听端口
  - `token() -> str`——本次启动的随机 token，供原生层拼 WebView URL
  - `interrupted_notice() -> Optional[str]`——上次被中断时的提示文案，无中断返回 `None`

- [ ] **Step 1: 写失败测试**

```python
def test_start_returns_listening_port_and_sets_cwd(self):
    # overrides 指向临时目录；断言返回端口 > 0，且 cwd 已切到 data_dir
    ...

def test_token_is_random_per_process(self):
    self.assertNotEqual(android_main.token(), "")
    self.assertEqual(len(set([android_main.token(), android_main.token()])), 1)  # 同进程内稳定

def test_interrupted_notice_present_after_dirty_journal(self):
    # 预置一条未 mark_clean_exit 的日志，断言返回的文案包含文件名
    ...
```

- [ ] **Step 2: 跑测试确认失败**

Run: `py.test tests/test_android_bootstrap.py -v`
Expected: FAIL，`ModuleNotFoundError: android_main`

- [ ] **Step 3: 实现 `android_main.py`**

严格照搬 `gui_main.py:139-224` 的顺序：先 `os.chdir(data_dir)`，**再** `import media_downloader`；然后 `app.load_config()` → `app.pre_run()` → 起 asyncio 线程 → 构造 `Controller` → `register_gui(controller, token, native=None)` → `make_web_server("127.0.0.1", app.web_port)` → `serve_forever` 线程 → `controller.start()`。

`native=None` 是有意为之：`module/web.py:108-118` 的文档说明，不传 `native` 时 `/api/native/*` 直接 404，正是我们要的（目录选择器已按设计文档 §7 移除）。

- [ ] **Step 4: 跑测试确认通过**

Run: `py.test tests/test_android_bootstrap.py -v`
Expected: PASS

- [ ] **Step 5: 写 `requirements-android.txt`**

以 `requirements.txt` 为基准，逐行标注偏离原因。已知必须偏离的三项见 Global Constraints。`PyYAML==5.3.1` 在现代工具链上有已知 sdist 构建问题，若 Chaquopy 装不上则上浮并在文件中注明。不含 `pywebview`、`pyinstaller`。

- [ ] **Step 6: 提交**

```bash
git add android_main.py requirements-android.txt tests/test_android_bootstrap.py
git commit -m "feat: 新增 Android 入口与依赖清单"
```

---

## Task 5: Gradle 工程与依赖验证

把探路工程的已验证配置固化成正式工程，并把 `requirements-android.txt` 的全部依赖在模拟器上验证一遍。

**Files:**
- Create: `android/settings.gradle`、`android/build.gradle`、`android/gradle.properties`
- Create: `android/app/build.gradle`
- Create: `android/app/src/main/python/probe_deps.py`

**Interfaces:**
- Consumes: `requirements-android.txt`（Task 4）
- Produces: 可构建的 debug APK；`probe_deps.run() -> str` 返回每个依赖的导入结果

- [ ] **Step 1: 搭 Gradle 工程**

配置照抄设计文档附录的已验证版本（Chaquopy 15.0.1、AGP 8.6.1、Python 3.11、`abiFilters "arm64-v8a"`）。Python 源码目录指向仓库根，使 `module/` 与 `media_downloader.py` 直接可用。

- [ ] **Step 2: 写依赖探针**

`probe_deps.py` 逐个导入 `requirements-android.txt` 中的每个包，捕获异常并返回逐行 `OK` / `FAIL` 文本，格式与探路工程一致。

- [ ] **Step 3: 构建并在模拟器上运行探针**

```bash
export JAVA_HOME=~/.jdk/jdk-17.0.21+9/Contents/Home ANDROID_HOME=~/Library/Android/sdk
cd android && gradle assembleDebug
adb -s emulator-5554 install -r -t app/build/outputs/apk/debug/app-debug.apk
adb -s emulator-5554 logcat -d -s PROBE:I
```

Expected: 每个依赖一行 `OK`。`PyYAML` 失败则上浮版本并在 `requirements-android.txt` 注明，重跑本步。

- [ ] **Step 4: 验证 fork 版 pyrogram**

先尝试在 `pip { install }` 中直接写 GitHub zip URL。失败则按设计文档 §7 把 `tangyoha/pyrogram` 的 `patch` 分支 vendor 进仓库并加入 Python path，重跑 Step 3。

- [ ] **Step 5: 排除 bot 与 cloud_drive**

在 Chaquopy 的 `sourceSets` 中排除 `module/bot.py` 与 `module/cloud_drive.py`，然后重跑探针确认 `module` 包的导入链未被破坏。

- [ ] **Step 6: 提交**

```bash
git add android/ && git commit -m "feat: Android Gradle 工程与依赖验证"
```

---

## Task 6: 前台服务

托住 Flask 与下载循环，使切后台和锁屏后任务继续。

**Files:**
- Create: `android/app/src/main/java/.../DownloadService.kt`
- Modify: `android/app/src/main/AndroidManifest.xml`

**Interfaces:**
- Consumes: `android_main.start(overrides)`、`android_main.token()`（Task 4）
- Produces: `DownloadService.start(context, overrides): Unit`；服务启动后通过 `LocalBroadcast` 广播实际端口与 token

- [ ] **Step 1: 实现服务**

`foregroundServiceType="dataSync"`。在 `onCreate` 中经 Chaquopy 调用 `android_main.start()`，拿到端口后广播。常驻通知显示当前下载的文件名与进度。

**关键约束**：Activity 销毁不得终止服务——`onTaskRemoved` 不停服务，这是目标 3 的核心。

- [ ] **Step 2: 模拟器验证后台存活**

```bash
adb -s emulator-5554 shell am start -n <pkg>/.MainActivity
adb -s emulator-5554 shell input keyevent KEYCODE_HOME
adb -s emulator-5554 shell dumpsys activity services <pkg> | grep -i "isForeground"
```

Expected: 回到桌面后服务仍为 foreground；通知栏存在常驻通知。

- [ ] **Step 3: 提交**

```bash
git add android/ && git commit -m "feat: 前台服务托管下载，切后台继续运行"
```

---

## Task 7: WebView 壳

**Files:**
- Create: `android/app/src/main/java/.../MainActivity.kt`

**Interfaces:**
- Consumes: Task 6 广播的端口与 token
- Produces: 无（终端界面）

- [ ] **Step 1: 实现 Activity**

单 Activity 内嵌 WebView，加载 `http://127.0.0.1:<port>/?token=<token>`，URL 形态与 `gui_main.py:228` 一致。启用 JavaScript 与 DOM storage，对齐 pywebview 行为。

若 `android_main.interrupted_notice()` 非空，用原生 Snackbar 展示（不进 WebView，遵守"界面不分叉"约束）。

- [ ] **Step 2: 模拟器验证界面与桌面一致**

```bash
adb -s emulator-5554 shell am start -n <pkg>/.MainActivity
adb -s emulator-5554 exec-out screencap -p > /tmp/android_ui.png
```

Expected: 截图中的界面与桌面版 `index.html` 渲染结果一致（同一套模板）。

- [ ] **Step 3: 提交**

```bash
git add android/ && git commit -m "feat: WebView 壳复用桌面版界面"
```

---

## Task 8: 存储权限与引导

**Files:**
- Create: `android/app/src/main/java/.../OnboardingActivity.kt`
- Create: `android/app/src/main/java/.../StorageBridge.kt`
- Modify: `android/app/src/main/AndroidManifest.xml`

**Interfaces:**
- Produces:
  - `StorageBridge.hasAllFilesAccess(context): Boolean`
  - `StorageBridge.pathOverrides(context): Map<String, String>`——喂给 `android_main.start()`，键名须与 `Paths` 字段一致（`data_dir` / `save_path` / `temp_dir` / `log_dir`）

- [ ] **Step 1: 实现权限检查与引导页**

声明 `MANAGE_EXTERNAL_STORAGE`。未授权时停在引导页，跳 `ACTION_MANAGE_APP_ALL_FILES_ACCESS_PERMISSION`。

**权限被拒时必须明确提示并停在引导页**（Review Focus 4）——不得放行进主界面，否则下载会静默失败，用户无从判断原因。

同页引导一加的电源管理设置（电池不受限制、允许自启动），文案说明这是系统限制、代码无法代劳。

- [ ] **Step 2: 实现路径注入**

`save_path` 指向共享存储的普通目录；`temp_dir` 指向 app 私有目录（避免半成品进相册）；`data_dir`、`log_dir` 均在私有目录。

- [ ] **Step 3: 模拟器验证两条路径**

```bash
adb -s emulator-5554 shell appops set --uid <pkg> MANAGE_EXTERNAL_STORAGE deny
# Expected: 停在引导页，有明确提示
adb -s emulator-5554 shell appops set --uid <pkg> MANAGE_EXTERNAL_STORAGE allow
# Expected: 进入主界面
```

- [ ] **Step 4: 提交**

```bash
git add android/ && git commit -m "feat: 共享存储权限与一加电源管理引导"
```

---

## Task 9: 媒体库注册

让下载完的文件在相册里直接可见。

**Files:**
- Modify: `android/app/src/main/java/.../StorageBridge.kt`
- Modify: `module/platform_paths.py`（新增完成回调钩子）
- Modify: `media_downloader.py:97-110`（`_move_to_download_path` 完成后触发回调）
- Test: `tests/module/test_platform_paths.py`（追加）

**Interfaces:**
- Consumes: `Paths`（Task 1）
- Produces: `StorageBridge.scanFile(path: String): Unit`——经 `MediaScannerConnection` 注册单个文件

- [ ] **Step 1: 写去重的失败测试**

```python
def test_same_path_scanned_twice_registers_once(self):
    # 对同一路径调用两次完成回调，断言底层 scan 钩子只收到一次
    ...
```

对应 Review Focus 5：续传完成与重复下载都可能对同一路径触发两次扫描，相册里不应出现两条同路径记录。

- [ ] **Step 2: 跑测试确认失败**

Run: `py.test tests/module/test_platform_paths.py -k scanned_twice -v`
Expected: FAIL

- [ ] **Step 3: 实现完成回调与去重**

Python 侧在 `_move_to_download_path`（`media_downloader.py:97-110`）完成后触发回调；回调在桌面为空操作，在 Android 转调 `StorageBridge.scanFile`。按已扫描路径集合去重。

- [ ] **Step 4: 跑测试确认通过**

Run: `py.test tests/module/test_platform_paths.py -v`
Expected: PASS

- [ ] **Step 5: 模拟器验证相册可见**

下载一个文件后：

```bash
adb -s emulator-5554 shell content query --uri content://media/external/video/media --projection _data | grep <filename>
```

Expected: 恰好一条记录。

- [ ] **Step 6: 提交**

```bash
git add android/ module/platform_paths.py media_downloader.py tests/module/test_platform_paths.py
git commit -m "feat: 下载完成后注册到媒体库，相册直接可见"
```

---

## Task 10: 保存路径输入框置为只读

目录选择器已移除（设计文档 §7），模板中的自由输入框在 Android 下不应再可编辑。

**Files:**
- Modify: `module/templates/gui_panels.html:43-44`
- Modify: `module/web.py:231`（扩展现有 `native_mode` 标志）
- Test: `tests/module/test_web_api.py`（追加）

**Interfaces:**
- Consumes: 无
- Produces: 模板上下文新增 `platform` 变量，取值 `"desktop"` 或 `"android"`

- [ ] **Step 1: 写失败测试**

```python
def test_android_platform_renders_save_path_readonly(self):
    # 以 platform="android" 渲染，断言 save_path 输入框带 readonly
    ...

def test_desktop_platform_keeps_save_path_editable(self):
    # 桌面不回归
    ...
```

- [ ] **Step 2: 跑测试确认失败**

Run: `py.test tests/module/test_web_api.py -k save_path -v`
Expected: FAIL

- [ ] **Step 3: 实现**

复用 `index.html:20` 的 `data-native` 既有模式，不新增模板文件。

- [ ] **Step 4: 跑测试确认通过**

Run: `py.test tests/module/test_web_api.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add module/templates/gui_panels.html module/web.py tests/module/test_web_api.py
git commit -m "feat: Android 下保存路径只读"
```

---

## Task 11: 端到端验收

**Files:** 无新增

- [ ] **Step 1: 跑桌面全套测试确认无回归**

Run: `make test`
Expected: PASS

- [ ] **Step 2: 模拟器上逐项验证**

按设计文档 §8 的清单：登录、建任务、大小限制、起始月份、skipped 统计、备份到 Saved Messages、切后台继续、锁屏继续、杀进程后重启提示中断并续传。

杀进程验证：

```bash
adb -s emulator-5554 shell am force-stop <pkg>
adb -s emulator-5554 shell am start -n <pkg>/.MainActivity
```

Expected: 界面出现"上次下载被系统中断"提示；未下完的文件从断点继续而非从头。

- [ ] **Step 3: 产出 release APK 交付真机**

厂商电源管理行为只能在真机验证，不作为模拟器验收项。

- [ ] **Step 4: 提交**

```bash
git commit -m "chore: Android APK 端到端验收通过"
```
