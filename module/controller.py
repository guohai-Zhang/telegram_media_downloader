"""GUI controller: owns the Telegram client and the app state machine.

Flask request threads call the public methods. Every Telegram call runs on
the asyncio loop thread through run_coroutine_threadsafe, so this class is
the only place where threads and asyncio meet.
"""

import asyncio
import concurrent.futures
import contextlib
import threading
from enum import Enum
from typing import Any, Callable, Dict, Iterator, List, Optional

import pyrogram
from loguru import logger
from pyrogram import raw
from pyrogram.errors import (
    ApiIdInvalid,
    ApiIdPublishedFlood,
    FloodWait,
    PasswordHashInvalid,
    PhoneCodeExpired,
    PhoneCodeInvalid,
    PhoneNumberInvalid,
    SessionPasswordNeeded,
    Unauthorized,
)

from module.gui_config import PHONE_ERROR, GuiError, InvalidState, normalize_phone

CONNECT_TIMEOUT = 20
CALL_TIMEOUT = 60
NETWORK_ERROR = "连接 Telegram 失败，请检查网络和代理设置"
API_ERROR = "API 凭证无效，请检查 api_id 和 api_hash"
REVOKED_NOTICE = "登录已失效，请重新登录"
_TIMEOUTS = (asyncio.TimeoutError, concurrent.futures.TimeoutError, OSError)
# errors that only need a message; the state stays where it is
_SIMPLE_ERRORS = (
    (PhoneNumberInvalid, PHONE_ERROR),
    (PhoneCodeInvalid, "验证码不正确"),
    (PasswordHashInvalid, "两步验证密码不正确"),
)


class State(Enum):
    """Controller states; the value is what the web page sees."""

    NEED_CONFIG = "need_config"
    CONNECTING = "connecting"
    ERROR = "error"
    LOGGED_OUT = "logged_out"
    CODE_SENT = "code_sent"
    NEED_PASSWORD = "need_password"
    READY = "ready"
    DOWNLOADING = "downloading"
    STOPPING = "stopping"


def describe_chat(chat: Any) -> Dict[str, Any]:
    """The fields of a pyrogram Chat that the page needs."""
    return {
        "id": chat.id,
        "title": chat.title or chat.first_name or str(chat.id),
        "type": chat.type.name.lower(),
        "username": chat.username or "",
    }


def describe_user(user: Any) -> Dict[str, str]:
    """The fields of a pyrogram User that the page needs."""
    name = " ".join(part for part in (user.first_name, user.last_name) if part)
    return {"username": user.username or "", "name": name}


# pylint: disable = R0902
class Controller:
    """State machine behind the GUI; see the spec for the transition diagram."""

    def __init__(
        self,
        app: Any,
        loop: asyncio.AbstractEventLoop,
        client_factory: Callable[[], Any],
        downloader: Any,
        config_path: str,
    ):
        self._app = app
        self._loop = loop
        self._client_factory = client_factory
        self._downloader = downloader
        self._config_path = config_path
        self._client: Any = None
        self._state = State.NEED_CONFIG
        self._error = ""
        self._notice = ""
        self._password_hint = ""
        self._me: Optional[Dict[str, str]] = None
        self._phone = ""
        self._phone_code_hash = ""
        self._dialogs: Optional[List[Dict[str, Any]]] = None
        self._resolved: Dict[Any, Dict[str, Any]] = {}
        # guards the fields above; held only briefly, never across a Telegram call
        self._state_lock = threading.RLock()
        # one user operation at a time; a second click gets a 409 instead of queueing
        self._op_lock = threading.Lock()
        self._connect_future: Optional[concurrent.futures.Future] = None
        self._watch_future: Optional[concurrent.futures.Future] = None

    # ---- state helpers

    @property
    def state(self) -> State:
        """Current state."""
        with self._state_lock:
            return self._state

    def _set_state(self, state: State, error: str = "") -> None:
        """Change state; any state but NEED_PASSWORD clears the password hint."""
        with self._state_lock:
            self._state = state
            self._error = error
            if state is not State.NEED_PASSWORD:
                self._password_hint = ""

    def _require(self, *states: State) -> None:
        """Raise InvalidState unless the current state is one of `states`."""
        with self._state_lock:
            if self._state not in states:
                raise InvalidState()

    def _run(self, coro: Any, timeout: float = CALL_TIMEOUT) -> Any:
        """Run a coroutine on the loop thread and wait for its result."""
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout)

    @contextlib.contextmanager
    def _operation(self, *states: State) -> Iterator[None]:
        """Run one user operation: reject concurrent ones, check state, map errors."""
        if not self._op_lock.acquire(blocking=False):
            raise GuiError("正在处理上一个操作，请稍候", status=409)
        try:
            self._require(*states)
            try:
                yield
            except GuiError:
                raise
            except Exception as e:
                raise self._map_error(e) from e
        finally:
            self._op_lock.release()

    def _map_error(self, e: BaseException, change_state: bool = True) -> GuiError:
        """Translate an exception into a user-facing error, updating state if needed."""
        if isinstance(e, FloodWait):
            return GuiError(f"操作太频繁，请 {e.value} 秒后再试")
        for error_type, message in _SIMPLE_ERRORS:
            if isinstance(e, error_type):
                return GuiError(message)
        if isinstance(e, (ApiIdInvalid, ApiIdPublishedFlood)):
            if change_state:
                self._set_state(State.NEED_CONFIG, API_ERROR)
            return GuiError(API_ERROR)
        if isinstance(e, _TIMEOUTS):
            if change_state:
                self._set_state(State.ERROR, NETWORK_ERROR)
            return GuiError(NETWORK_ERROR, status=502)
        logger.exception(e)
        message = f"出错了：{type(e).__name__}，详情见日志"
        if change_state:
            self._set_state(State.ERROR, message)
        return GuiError(message, status=500)

    # ---- status

    def status(self) -> Dict[str, Any]:
        """Snapshot for the page's 1-second poll."""
        with self._state_lock:
            configs = list(self._app.chat_download_config.values())
            return {
                "state": self._state.value,
                "error": self._error,
                "notice": self._notice,
                "password_hint": self._password_hint,
                "me": self._me,
                "chat_count": len(configs),
                "progress": {
                    "total": sum(c.node.total_task for c in configs),
                    "done": sum(c.finish_task for c in configs),
                },
            }

    def set_notice(self, notice: str) -> None:
        """Show a one-off message in the status bar (e.g. config was repaired)."""
        with self._state_lock:
            self._notice = notice

    # ---- connection

    def start(self) -> None:
        """Called once at startup: connect if credentials are configured."""
        if self._app.api_id and self._app.api_hash:
            self._begin_connect()
        else:
            self._set_state(State.NEED_CONFIG)

    def retry(self) -> None:
        """Reconnect after a connection error.

        Goes through `_operation` so a second concurrent click gets the 409
        "正在处理上一个操作" instead of racing `_begin_connect` and starting
        two connects.
        """
        with self._operation(State.ERROR):
            self._begin_connect()

    def _begin_connect(self) -> None:
        """Start (or restart) connecting in the background."""
        with self._state_lock:
            if self._connect_future is not None and not self._connect_future.done():
                self._connect_future.cancel()
            self._state = State.CONNECTING
            self._error = ""
            self._me = None
            self._dialogs = None
            self._connect_future = asyncio.run_coroutine_threadsafe(
                self._connect(), self._loop
            )

    async def _connect(self) -> None:
        """Create a fresh client and connect; ends in READY, LOGGED_OUT or ERROR.

        CONNECT_TIMEOUT bounds `client_factory()` + `client.connect()` *and*,
        when the session is already authorized, the whole finish-login
        sequence that follows — pyrogram's own retry loop can otherwise keep
        GetState/get_me spinning for minutes after a proxy drops the
        connection mid-handshake. Creating the client is inside the guarded
        block too, so a `client_factory()` failure ends in ERROR instead of
        leaving an exception on an unread future.
        """
        await self._disconnect()
        client = None
        try:
            client = self._client_factory()
            self._client = client
            await asyncio.wait_for(self._connect_and_login(client), CONNECT_TIMEOUT)
        except Exception as e:
            await self._handle_connect_failure(client, e)

    async def _connect_and_login(self, client: Any) -> None:
        """Connect `client` and, if it is already authorized, finish login."""
        authorized = await client.connect()
        if authorized:
            await self._finish_login()
        else:
            self._set_state(State.LOGGED_OUT)

    async def _handle_connect_failure(self, client: Any, e: BaseException) -> None:
        """Clean up after a failed/cancelled connect and land in ERROR.

        A revoked session is detected either directly — `e` is an
        `Unauthorized` error other than `SessionPasswordNeeded` — or
        indirectly: cleaning up a half-open client can itself surface one
        (pyrogram's transport -404 `Unauthorized` raised from inside
        `session.stop()` after a Ping timeout, which the outer
        CONNECT_TIMEOUT would otherwise turn into a plain NETWORK_ERROR
        forever). Either way, `_handle_revoked_session` runs instead of the
        generic ERROR path below.
        """
        direct = isinstance(e, Unauthorized) and not isinstance(
            e, SessionPasswordNeeded
        )
        cleanup_revoked = await self._cleanup_client(client)
        if self._client is client:
            self._client = None
        if direct or cleanup_revoked:
            await self._handle_revoked_session(client)
            return
        error = self._map_error(e)
        with self._state_lock:
            if self._state is State.CONNECTING:
                self._state, self._error = State.ERROR, error.message

    async def _handle_revoked_session(self, old_client: Any) -> None:
        """The saved login is no longer valid: drop it and start over.

        `old_client` has already been cleaned up by the caller. This
        deletes its local session storage (best-effort) and hands off to
        `_reconnect_after_revoke` for the fresh, timeout-bounded reconnect.
        """
        storage = getattr(old_client, "storage", None)
        if storage is not None:
            await self._safe_call(storage.delete, "storage.delete")
        await self._reconnect_after_revoke()

    async def _reconnect_after_revoke(self) -> None:
        """Create and connect a fresh, unauthorized client after a revoke.

        Bounded by CONNECT_TIMEOUT (creating the client included, same
        reasoning as `_connect`): success lands in LOGGED_OUT with the
        revoked-session notice; any failure or timeout — including one
        stuck creating a brand-new auth key against a dead network — cleans
        up the fresh client and lands in ERROR with NETWORK_ERROR instead of
        hanging in CONNECTING or leaking that client.
        """
        fresh_client = None
        try:
            fresh_client = self._client_factory()
            self._client = fresh_client
            await asyncio.wait_for(fresh_client.connect(), CONNECT_TIMEOUT)
        except Exception as e:
            await self._cleanup_client(fresh_client)
            if self._client is fresh_client:
                self._client = None
            logger.warning(f"reconnect after revoked session failed: {e}")
            self._set_state(State.ERROR, NETWORK_ERROR)
            return
        self._set_state(State.LOGGED_OUT)
        self.set_notice(REVOKED_NOTICE)

    async def _finish_login(self) -> None:
        """The part of pyrogram's Client.start() that runs after authorization."""
        client = self._client
        await client.invoke(raw.functions.updates.GetState())
        client.me = await client.get_me()
        await client.initialize()
        self._me = describe_user(client.me)
        self._set_state(State.READY)

    async def _disconnect(self) -> None:
        """Stop and forget the current client, ignoring errors."""
        client, self._client = self._client, None
        await self._cleanup_client(client)

    async def _cleanup_client(self, client: Any) -> bool:
        """Best-effort release of a client's resources; never raises.

        Returns whether cleanup itself surfaced a revoked-session
        `Unauthorized` (other than `SessionPasswordNeeded`) — pyrogram can
        raise that from `session.stop()` (see `_handle_connect_failure`).

        `terminate()`/`disconnect()` are tried first, matching real
        pyrogram's own teardown (only a successful `disconnect()` closes
        the session/storage internally there). `session.stop()`/
        `storage.close()` are then tried too as a fallback whenever: the
        client was never connected/initialized in the first place (the
        half-open client from I2 — neither call even runs), or `terminate()`
        /`disconnect()` was attempted but raised partway through (F4) —
        either way the session/storage handle would otherwise leak.
        """
        if client is None:
            return False
        revoked = False
        initialized = getattr(client, "is_initialized", False)
        connected = getattr(client, "is_connected", False)
        terminate_failed = False
        if initialized:
            err = await self._safe_call(client.terminate, "terminate")
            revoked = revoked or self._is_revoked(err)
            terminate_failed = err is not None
        disconnect_failed = False
        if connected:
            err = await self._safe_call(client.disconnect, "disconnect")
            revoked = revoked or self._is_revoked(err)
            disconnect_failed = err is not None
        never_opened = not initialized and not connected
        if never_opened or terminate_failed or disconnect_failed:
            revoked = await self._cleanup_session_storage(client) or revoked
        return revoked

    async def _cleanup_session_storage(self, client: Any) -> bool:
        """Fallback half of `_cleanup_client`: stop the session, close storage."""
        revoked = False
        session = getattr(client, "session", None)
        if session is not None:
            err = await self._safe_call(session.stop, "session.stop")
            revoked = revoked or self._is_revoked(err)
        storage = getattr(client, "storage", None)
        if storage is not None:
            err = await self._safe_call(storage.close, "storage.close")
            revoked = revoked or self._is_revoked(err)
        return revoked

    @staticmethod
    def _is_revoked(err: Optional[BaseException]) -> bool:
        """Whether `err` is a revoked-session `Unauthorized` (not a password one)."""
        return isinstance(err, Unauthorized) and not isinstance(
            err, SessionPasswordNeeded
        )

    @staticmethod
    async def _safe_call(
        func: Callable[[], Any], label: str
    ) -> Optional[BaseException]:
        """Await `func()`, logging (not raising) on failure; returns the error, if any."""
        try:
            await func()
        except Exception as e:
            logger.warning(f"{label} failed: {e}")
            return e
        return None

    # ---- login

    def send_code(self, phone: str) -> None:
        """Step 1: ask Telegram to send a login code."""
        phone = normalize_phone(phone)
        with self._operation(State.LOGGED_OUT, State.CODE_SENT):
            sent = self._run(self._client.send_code(phone))
            self._phone, self._phone_code_hash = phone, sent.phone_code_hash
            self._set_state(State.CODE_SENT)

    def sign_in(self, code: str) -> None:
        """Step 2: sign in with the code; may require the two-step password."""
        code = "".join((code or "").split())
        if not code:
            raise GuiError("请输入验证码")
        with self._operation(State.CODE_SENT):
            try:
                result = self._run(
                    self._client.sign_in(self._phone, self._phone_code_hash, code)
                )
            except SessionPasswordNeeded:
                hint = self._run(self._client.get_password_hint())
                self._set_state(State.NEED_PASSWORD)
                with self._state_lock:
                    self._password_hint = hint or ""
                return
            except PhoneCodeExpired as e:
                self._set_state(State.LOGGED_OUT)
                raise GuiError("验证码已过期，请重新获取") from e
            # sign_in returns TermsOfService or False when the number is not registered
            if result is False or isinstance(result, pyrogram.types.TermsOfService):
                self._set_state(State.LOGGED_OUT)
                raise GuiError("该手机号还没有注册 Telegram，请先在手机上注册")
            self._run(self._finish_login())

    def check_password(self, password: str) -> None:
        """Step 3: the two-step verification password."""
        if not password:
            raise GuiError("请输入两步验证密码")
        with self._operation(State.NEED_PASSWORD):
            self._run(self._client.check_password(password))
            self._run(self._finish_login())

    def log_out(self) -> None:
        """Log out (deletes the session) and go back to the phone step."""
        with self._operation(State.READY):
            self._run(self._client.log_out())
            self._client = None
        self._begin_connect()
