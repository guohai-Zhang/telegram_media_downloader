"""Entry point of the macOS GUI app (TelegramDownloader.app).

Order matters: the working directory must point at the data directory before
media_downloader is imported, because Application() resolves config, session,
log and temp paths from the current directory at construction time.
"""

import os
import secrets
import subprocess
import sys
import threading
from typing import IO, Any, Optional

from loguru import logger

from module.gui_config import ensure_config_file, ensure_data_file

APP_DIR_NAME = "TelegramMediaDownloader"
WINDOW_TITLE = "Telegram 下载器"
QUIT_CONFIRM_MESSAGE = "正在下载，退出后进度会保存，下次可以继续。确定退出吗？"
LOG_ROTATION = "10 MB"
LOG_RETENTION = "10 days"
# title and message arrive as arguments, so no text is ever parsed as AppleScript
_ALERT_SCRIPT = (
    "on run argv",
    "display alert (item 1 of argv) message (item 2 of argv) giving up after 15",
    "end run",
)


def _home(home: Optional[str] = None) -> str:
    """Home directory; TDL_GUI_HOME lets dev runs and smoke tests use a scratch dir."""
    return home or os.environ.get("TDL_GUI_HOME") or os.path.expanduser("~")


def data_dir(home: Optional[str] = None) -> str:
    """Where config, sessions, logs and temp files live."""
    return os.path.join(_home(home), "Library", "Application Support", APP_DIR_NAME)


def default_save_path(home: Optional[str] = None) -> str:
    """Default download folder for a fresh install."""
    return os.path.join(_home(home), "Downloads", "Telegram")


def acquire_single_instance_lock(directory: str) -> Optional[IO[str]]:
    """Return the open lock file, or None if another instance already holds it."""
    import fcntl  # pylint: disable = import-outside-toplevel

    handle = open(  # pylint: disable = consider-using-with
        os.path.join(directory, ".lock"), "w", encoding="utf-8"
    )
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


def ensure_std_streams() -> None:
    """Windowed apps may start without stdout/stderr; logging needs somewhere to write."""
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")  # pylint: disable = R1732
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")  # pylint: disable = R1732


class NativeActions:
    """Backs the /api/native/* routes (module.web); never exposed to page JS.

    Earlier this was a pywebview js_api object (window.pywebview.api.*), but
    pywebview's JS bridge resolves any dotted attribute path with plain
    getattr - even a "private" `_window` attribute is reachable from the
    page, and through it the whole Python object graph (e.g.
    `_window.gui.os.system`). Removing js_api entirely and driving these two
    native actions through ordinary token-guarded Flask routes closes that
    hole; a Flask request thread can safely call pywebview APIs like
    `create_file_dialog`, which round-trip to the main thread internally.
    """

    def __init__(self, log_dir: str):
        self._log_dir = log_dir
        self._window: Any = None

    def attach(self, window: Any) -> None:
        """Set once the window exists."""
        self._window = window

    def choose_folder(self) -> Optional[str]:
        """Native folder picker for the save path."""
        import webview  # pylint: disable = import-outside-toplevel

        result = self._window.create_file_dialog(webview.FileDialog.FOLDER)
        return result[0] if result else None

    def open_log_folder(self) -> None:
        """Reveal the log folder in Finder so users can send logs."""
        os.makedirs(self._log_dir, exist_ok=True)
        subprocess.run(["open", self._log_dir], check=False)


def _alert(message: str) -> None:
    """Native alert for problems that happen before the window exists.

    `message` can hold arbitrary text (paths, exception names), so it is
    passed to the AppleScript run handler as an argument instead of being
    pasted into the script. `giving up after 15` makes the dialog
    auto-dismiss so an unattended run (e.g. the smoke test's second
    instance) cannot hang waiting for a click.
    """
    command = ["osascript"]
    for line in _ALERT_SCRIPT:
        command += ["-e", line]
    subprocess.run(command + [WINDOW_TITLE, message], check=False)


def _add_log_file(log_dir: str, level: Any) -> int:
    """Add the tdl.log sink (rotated like the CLI's) and return its loguru id."""
    sink_id: int = logger.add(
        os.path.join(log_dir, "tdl.log"),
        rotation=LOG_ROTATION,
        retention=LOG_RETENTION,
        level=level,
    )
    return sink_id


def main() -> int:
    """Take the single-instance lock, then run the app; report startup failures.

    Everything after the lock runs inside one try block: an error before the
    window opens (a config the loader chokes on, a port setting that is not
    a number, ...) would otherwise kill the app silently at every launch.
    It is logged to tdl.log - whose sink is added first thing - and shown
    in a native alert instead.
    """
    ensure_std_streams()
    directory = data_dir()
    os.makedirs(directory, exist_ok=True)
    os.chdir(directory)

    lock = acquire_single_instance_lock(directory)
    if lock is None:
        _alert("已经在运行了，请切换到已打开的窗口。")
        return 1

    log_dir = os.path.join(directory, "log")
    try:
        os.makedirs(log_dir, exist_ok=True)
        sink_id = _add_log_file(log_dir, "INFO")
        return _run(directory, log_dir, sink_id)
    except Exception as e:  # pylint: disable = broad-except
        logger.exception(e)
        _alert(f"启动失败：{type(e).__name__}，日志在 {log_dir}")
        return 1
    finally:
        lock.close()


# pylint: disable = R0914, R0915
def _run(directory: str, log_dir: str, sink_id: int) -> int:
    """Start the loop, web server and window; clean up when the window closes."""
    config_path = os.path.join(directory, "config.yaml")
    notices = [
        ensure_config_file(config_path, default_save_path()),
        ensure_data_file(os.path.join(directory, "data.yaml")),
    ]
    notice = "；".join(n for n in notices if n)

    # pylint: disable = import-outside-toplevel
    import webview

    import media_downloader
    from module.controller import CONNECT_TIMEOUT, Controller, State
    from module.pyrogram_extension import HookClient
    from module.web import make_web_server, register_gui

    app = media_downloader.app
    app.load_config()
    app.pre_run()
    if str(app.log_level).upper() != "INFO":
        # add the configured level before dropping the early INFO sink, so a
        # bad log_level still leaves a sink to record the error
        _add_log_file(log_dir, app.log_level)
        logger.remove(sink_id)

    threading.Thread(target=app.loop.run_forever, daemon=True, name="asyncio").start()

    def make_client():
        return HookClient(
            "media_downloader",
            api_id=app.api_id,
            api_hash=app.api_hash,
            proxy=app.proxy or None,
            workdir=app.session_file_path,
            start_timeout=CONNECT_TIMEOUT,
            no_updates=True,
        )

    controller = Controller(
        app=app,
        loop=app.loop,
        client_factory=make_client,
        downloader=media_downloader,
        config_path=config_path,
    )
    if notice:
        controller.set_notice(notice)

    native = NativeActions(app.log_file_path)
    token = os.environ.get("TDL_GUI_TOKEN") or secrets.token_urlsafe(16)
    register_gui(controller, token, native=native)
    server, port = make_web_server("127.0.0.1", app.web_port)
    threading.Thread(target=server.serve_forever, daemon=True, name="web").start()
    logger.info(f"GUI web server listening on 127.0.0.1:{port}")
    controller.start()

    window = webview.create_window(
        WINDOW_TITLE,
        f"http://127.0.0.1:{port}/?token={token}",
        width=1000,
        height=720,
        min_size=(760, 520),
    )
    native.attach(window)

    cleanup_done = threading.Event()

    def cleanup() -> None:
        """Save progress and disconnect; idempotent so it can run twice.

        Called from `on_closing` (window close button, menu/Dock Quit,
        logout - everything that goes through `should_close`) and again
        after `webview.start()` returns unconditionally, which is the only
        place that also catches Cmd+Q handled directly by
        `WebKitHost.keyDown_` (-> `app.stop_`), a path that bypasses
        `should_close`/`events.closing` entirely.
        """
        if cleanup_done.is_set():
            return
        cleanup_done.set()
        try:
            controller.shutdown(timeout=10)
        except Exception as e:  # pylint: disable = broad-except
            logger.warning(f"cleanup: controller.shutdown failed: {e}")
        try:
            server.shutdown()
            server.server_close()
        except Exception as e:  # pylint: disable = broad-except
            logger.warning(f"cleanup: web server did not stop cleanly: {e}")
        try:
            app.loop.call_soon_threadsafe(app.loop.stop)
        except Exception as e:  # pylint: disable = broad-except
            logger.warning(f"cleanup: asyncio loop did not stop cleanly: {e}")
        logger.info("GUI stopped")

    def on_closing() -> bool:
        """window.events.closing handler.

        pywebview runs `closing` handlers synchronously on the main thread
        for both the window's close button and an app-level quit routed
        through `applicationShouldTerminate_` -> `should_close`, so a modal
        confirmation dialog here is safe (pywebview is pinned at 6.2.1;
        this relies on that internal behavior). `window.confirm_close` is
        left at its default False so pywebview's own post-`closing`
        confirmation (driven by the `global.quitConfirmation`
        localization) never fires a second dialog.
        """
        if controller.state is State.DOWNLOADING:
            # pylint: disable = import-outside-toplevel
            from webview.platforms.cocoa import BrowserView

            confirmed = BrowserView.display_confirmation_dialog(
                "退出", "取消", QUIT_CONFIRM_MESSAGE
            )
            if not confirmed:
                return False
        cleanup()
        return True

    window.events.closing += on_closing

    try:
        webview.start()
    finally:
        # webview.start() can also return via the keyDown_ -> app.stop_ path,
        # which never runs on_closing; cleanup() is a no-op if it already ran.
        cleanup()
    return 0


if __name__ == "__main__":
    sys.exit(main())
