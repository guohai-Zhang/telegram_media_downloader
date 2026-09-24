"""test controller state machine with a fake Telegram client"""

import asyncio
import os
import shutil
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from pyrogram import errors
from pyrogram import sync as pyrogram_sync
from pyrogram.enums import ChatType

from module.app import Application
from module.controller import NETWORK_ERROR, Controller, State
from module.gui_config import (
    GuiError,
    InvalidState,
    ensure_config_file,
    read_chats,
    write_chats,
)

HASH = "0123456789abcdef0123456789abcdef"


class FakeSession:
    """Stands in for pyrogram's Session; records stop() calls."""

    def __init__(self):
        self.stop_calls = 0
        self.stop_error = None

    async def stop(self):
        self.stop_calls += 1
        if self.stop_error:
            raise self.stop_error


class FakeStorage:
    """Stands in for pyrogram's Storage; records close()/delete() calls."""

    def __init__(self):
        self.close_calls = 0
        self.delete_calls = 0

    async def close(self):
        self.close_calls += 1

    async def delete(self):
        self.delete_calls += 1


class FakeClient:
    """Stands in for HookClient; tests tweak the attributes they need."""

    def __init__(self):
        self.authorized = False
        self.connect_delay = 0.0
        self.connect_error = None
        self.send_code_error = None
        self.sign_in_result = "user"
        self.password_error = None
        self.password_hint = "hint"
        self.dialogs = []
        self.chats = {}
        self.is_connected = False
        self.is_initialized = False
        self.me = None
        self.calls = []
        # session is created by connect(), like real pyrogram, before it
        # awaits anything; storage exists for the client's whole lifetime.
        self.session = None
        self.storage = FakeStorage()
        self.invoke_error = None
        self.finish_login_delay = 0.0
        self.terminate_error = None
        self.disconnect_error = None
        # carried onto whatever FakeSession connect() creates next, so it's
        # set before start() even if connect() hasn't run yet.
        self.session_stop_error = None

    async def connect(self):
        self.calls.append("connect")
        self.session = FakeSession()
        self.session.stop_error = self.session_stop_error
        if self.connect_delay:
            await asyncio.sleep(self.connect_delay)
        if self.connect_error:
            raise self.connect_error
        self.is_connected = True
        return self.authorized

    async def disconnect(self):
        self.calls.append("disconnect")
        self.is_connected = False
        if self.disconnect_error:
            error, self.disconnect_error = self.disconnect_error, None
            raise error

    async def terminate(self):
        self.calls.append("terminate")
        self.is_initialized = False
        if self.terminate_error:
            error, self.terminate_error = self.terminate_error, None
            raise error

    async def invoke(self, query):
        self.calls.append(type(query).__name__)
        if self.finish_login_delay:
            await asyncio.sleep(self.finish_login_delay)
        if self.invoke_error:
            raise self.invoke_error

    async def get_me(self):
        return SimpleNamespace(username="alice", first_name="Alice", last_name=None)

    async def initialize(self):
        self.is_initialized = True

    async def send_code(self, phone):
        self.calls.append(("send_code", phone))
        if self.send_code_error:
            raise self.send_code_error
        return SimpleNamespace(phone_code_hash="hash123")

    async def sign_in(self, phone, phone_code_hash, code):
        self.calls.append(("sign_in", phone, phone_code_hash, code))
        if isinstance(self.sign_in_result, BaseException):
            raise self.sign_in_result
        return self.sign_in_result

    async def get_password_hint(self):
        return self.password_hint

    async def check_password(self, password):
        self.calls.append(("check_password", password))
        if self.password_error:
            error, self.password_error = self.password_error, None
            raise error
        return "user"

    async def log_out(self):
        self.calls.append("log_out")
        self.authorized = False
        self.is_connected = False

    async def get_dialogs(self):
        self.calls.append("get_dialogs")
        for dialog in self.dialogs:
            yield dialog

    async def get_chat(self, username):
        if username not in self.chats:
            raise errors.UsernameNotOccupied()
        return self.chats[username]


class FakeDownloader:
    """Stands in for the media_downloader module."""

    def __init__(self):
        self.finished = None
        self.started_with = None
        self.stop_calls = 0
        self.stop_delay = 0.0
        self.stop_error = None

    async def start_download(self, client):
        self.started_with = client
        self.finished = asyncio.Event()

    async def wait_until_finished(self):
        await self.finished.wait()

    async def stop_download(self):
        self.stop_calls += 1
        if self.stop_delay:
            await asyncio.sleep(self.stop_delay)
        if self.stop_error:
            error, self.stop_error = self.stop_error, None
            raise error
        if self.finished is not None:
            self.finished.set()


def make_chat(chat_id, title, chat_type=ChatType.CHANNEL, username=""):
    return SimpleNamespace(
        id=chat_id, title=title, type=chat_type, username=username, first_name=None
    )


class ControllerTestBase(unittest.TestCase):
    def setUp(self):
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()
        self.tmp = tempfile.mkdtemp()
        self.config_path = os.path.join(self.tmp, "config.yaml")
        ensure_config_file(self.config_path, os.path.join(self.tmp, "downloads"))
        # Application() installs its own loop as the current one; later test modules
        # rely on asyncio.get_event_loop(), so that loop is deliberately left open.
        self.app = Application(self.config_path, os.path.join(self.tmp, "data.yaml"))
        self.app.load_config()
        self.client = FakeClient()
        self.downloader = FakeDownloader()
        self.controller = Controller(
            app=self.app,
            loop=self.loop,
            client_factory=lambda: self.client,
            downloader=self.downloader,
            config_path=self.config_path,
        )

    def tearDown(self):
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=5)
        self.loop.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def wait_for_state(self, state, timeout=5.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.controller.state is state:
                return
            time.sleep(0.01)
        self.fail(f"state is {self.controller.state}, expected {state}")

    def with_credentials(self):
        self.app.api_id = 12345
        self.app.api_hash = HASH

    def ready_controller(self):
        self.with_credentials()
        self.client.authorized = True
        self.controller.start()
        self.wait_for_state(State.READY)

    def logged_out_controller(self):
        self.with_credentials()
        self.controller.start()
        self.wait_for_state(State.LOGGED_OUT)


class ConnectAndLoginTestCase(ControllerTestBase):
    def test_start_without_credentials_needs_config(self):
        self.controller.start()
        self.assertIs(self.controller.state, State.NEED_CONFIG)
        self.assertEqual(self.client.calls, [])

    def test_start_with_session_goes_ready(self):
        self.ready_controller()
        status = self.controller.status()
        self.assertEqual(status["state"], "ready")
        self.assertEqual(status["me"], {"username": "alice", "name": "Alice"})
        self.assertIn("GetState", self.client.calls)
        self.assertTrue(self.client.is_initialized)

    def test_start_without_session_goes_logged_out(self):
        self.logged_out_controller()
        self.assertIsNone(self.controller.status()["me"])

    def test_login_with_code(self):
        self.logged_out_controller()
        self.controller.send_code("+86 138-0000-0000")
        self.assertIs(self.controller.state, State.CODE_SENT)
        self.assertIn(("send_code", "+8613800000000"), self.client.calls)
        self.controller.sign_in(" 12 345 ")
        self.assertIn(
            ("sign_in", "+8613800000000", "hash123", "12345"), self.client.calls
        )
        self.assertIs(self.controller.state, State.READY)

    def test_login_with_two_step_password(self):
        self.logged_out_controller()
        self.client.sign_in_result = errors.SessionPasswordNeeded()
        self.client.password_error = errors.PasswordHashInvalid()
        self.controller.send_code("+8613800000000")
        self.controller.sign_in("12345")
        self.assertIs(self.controller.state, State.NEED_PASSWORD)
        self.assertEqual(self.controller.status()["password_hint"], "hint")

        with self.assertRaises(GuiError) as ctx:
            self.controller.check_password("wrong")
        self.assertIn("两步验证密码不正确", ctx.exception.message)
        self.assertIs(self.controller.state, State.NEED_PASSWORD)

        self.controller.check_password("right")
        self.assertIs(self.controller.state, State.READY)
        self.assertEqual(self.controller.status()["password_hint"], "")

    def test_expired_code_returns_to_phone_step(self):
        self.logged_out_controller()
        self.client.sign_in_result = errors.PhoneCodeExpired()
        self.controller.send_code("+8613800000000")
        with self.assertRaises(GuiError) as ctx:
            self.controller.sign_in("12345")
        self.assertIn("过期", ctx.exception.message)
        self.assertIs(self.controller.state, State.LOGGED_OUT)

    def test_invalid_code_keeps_code_step(self):
        self.logged_out_controller()
        self.client.sign_in_result = errors.PhoneCodeInvalid()
        self.controller.send_code("+8613800000000")
        with self.assertRaises(GuiError) as ctx:
            self.controller.sign_in("00000")
        self.assertEqual(ctx.exception.message, "验证码不正确")
        self.assertIs(self.controller.state, State.CODE_SENT)

    def test_unregistered_phone(self):
        self.logged_out_controller()
        self.client.sign_in_result = False
        self.controller.send_code("+8613800000000")
        with self.assertRaises(GuiError) as ctx:
            self.controller.sign_in("12345")
        self.assertIn("还没有注册", ctx.exception.message)
        self.assertIs(self.controller.state, State.LOGGED_OUT)

    def test_flood_wait_message(self):
        self.logged_out_controller()
        self.client.send_code_error = errors.FloodWait(value=30)
        with self.assertRaises(GuiError) as ctx:
            self.controller.send_code("+8613800000000")
        self.assertIn("30 秒", ctx.exception.message)
        self.assertIs(self.controller.state, State.LOGGED_OUT)

    def test_invalid_phone_rejected_before_network(self):
        self.logged_out_controller()
        with self.assertRaises(GuiError):
            self.controller.send_code("abc")
        self.assertFalse([c for c in self.client.calls if c[0] == "send_code"])

    @mock.patch("module.controller.CONNECT_TIMEOUT", new=0.2)
    def test_connect_timeout_goes_error_then_retry(self):
        self.with_credentials()
        self.client.connect_delay = 5
        self.controller.start()
        self.wait_for_state(State.ERROR)
        self.assertEqual(self.controller.status()["error"], NETWORK_ERROR)

        self.client.connect_delay = 0
        self.client.authorized = True
        self.controller.retry()
        self.wait_for_state(State.READY)

    def test_api_id_invalid_goes_need_config(self):
        self.with_credentials()
        self.client.connect_error = errors.ApiIdInvalid()
        self.controller.start()
        self.wait_for_state(State.NEED_CONFIG)
        self.assertIn("API 凭证无效", self.controller.status()["error"])

    def test_operation_rejected_in_wrong_state(self):
        self.ready_controller()
        with self.assertRaises(InvalidState) as ctx:
            self.controller.sign_in("12345")
        self.assertEqual(ctx.exception.status, 409)

    def test_concurrent_operation_is_rejected(self):
        self.logged_out_controller()
        self.controller._op_lock.acquire()
        try:
            with self.assertRaises(GuiError) as ctx:
                self.controller.send_code("+8613800000000")
        finally:
            self.controller._op_lock.release()
        self.assertEqual(ctx.exception.status, 409)
        self.assertIn("正在处理", ctx.exception.message)

    def test_log_out_returns_to_logged_out(self):
        self.ready_controller()
        self.controller.log_out()
        self.wait_for_state(State.LOGGED_OUT)
        self.assertIn("log_out", self.client.calls)
        self.assertIsNone(self.controller.status()["me"])

    @mock.patch("module.controller.CONNECT_TIMEOUT", new=0.2)
    def test_concurrent_retry_is_rejected(self):
        self.with_credentials()
        self.client.connect_delay = 5
        self.controller.start()
        self.wait_for_state(State.ERROR)
        connect_calls_before = self.client.calls.count("connect")
        self.controller._op_lock.acquire()
        try:
            with self.assertRaises(GuiError) as ctx:
                self.controller.retry()
        finally:
            self.controller._op_lock.release()
        self.assertEqual(ctx.exception.status, 409)
        self.assertIn("正在处理", ctx.exception.message)
        self.assertIs(self.controller.state, State.ERROR)
        self.assertEqual(self.client.calls.count("connect"), connect_calls_before)

    @mock.patch("module.controller.CONNECT_TIMEOUT", new=0.2)
    def test_finish_login_timeout_goes_error(self):
        self.with_credentials()
        self.client.authorized = True
        self.client.finish_login_delay = 5
        self.controller.start()
        self.wait_for_state(State.ERROR)
        self.assertEqual(self.controller.status()["error"], NETWORK_ERROR)

    @mock.patch("module.controller.CONNECT_TIMEOUT", new=0.2)
    def test_connect_timeout_releases_half_open_client(self):
        self.with_credentials()
        self.client.connect_delay = 5
        self.controller.start()
        self.wait_for_state(State.ERROR)
        self.assertFalse(self.client.is_connected)
        self.assertFalse(self.client.is_initialized)
        self.assertGreaterEqual(self.client.session.stop_calls, 1)
        self.assertGreaterEqual(self.client.storage.close_calls, 1)

    def test_revoked_session_goes_logged_out_with_notice(self):
        self.with_credentials()
        self.client.authorized = True
        self.client.invoke_error = errors.AuthKeyUnregistered()
        fresh_client = FakeClient()
        factory_calls = []

        def factory():
            factory_calls.append(1)
            return self.client if len(factory_calls) == 1 else fresh_client

        self.controller._client_factory = factory
        self.controller.start()
        self.wait_for_state(State.LOGGED_OUT)
        self.assertEqual(self.controller.status()["notice"], "登录已失效，请重新登录")
        self.assertEqual(self.client.storage.delete_calls, 1)
        self.assertIs(self.controller._client, fresh_client)
        self.assertIn("connect", fresh_client.calls)

        # logging in again makes the notice stale
        self.controller.send_code("+8613800000000")
        self.assertEqual(self.controller.status()["notice"], "登录已失效，请重新登录")
        self.controller.sign_in("12345")
        self.assertIs(self.controller.state, State.READY)
        self.assertEqual(self.controller.status()["notice"], "")

    def test_two_step_login_clears_notice(self):
        self.logged_out_controller()
        self.controller.set_notice("配置文件损坏，已备份为 x 并恢复默认配置")
        self.client.sign_in_result = errors.SessionPasswordNeeded()
        self.controller.send_code("+8613800000000")
        self.controller.sign_in("12345")
        self.assertNotEqual(self.controller.status()["notice"], "")
        self.controller.check_password("right")
        self.assertIs(self.controller.state, State.READY)
        self.assertEqual(self.controller.status()["notice"], "")

    def test_automatic_login_at_launch_keeps_startup_notice(self):
        self.with_credentials()
        self.client.authorized = True
        self.controller.set_notice("下载记录文件损坏，已备份为 x 并重置")
        self.controller.start()
        self.wait_for_state(State.READY)
        self.assertEqual(self.controller.status()["notice"], "下载记录文件损坏，已备份为 x 并重置")

    @mock.patch("module.controller.CONNECT_TIMEOUT", new=0.2)
    def test_revoked_session_reconnect_timeout_goes_error(self):
        self.with_credentials()
        self.client.authorized = True
        self.client.invoke_error = errors.AuthKeyUnregistered()
        fresh_client = FakeClient()
        fresh_client.connect_delay = 5
        factory_calls = []

        def factory():
            factory_calls.append(1)
            return self.client if len(factory_calls) == 1 else fresh_client

        self.controller._client_factory = factory
        self.controller.start()
        self.wait_for_state(State.ERROR)
        self.assertEqual(self.controller.status()["error"], NETWORK_ERROR)
        self.assertGreaterEqual(fresh_client.session.stop_calls, 1)
        self.assertGreaterEqual(fresh_client.storage.close_calls, 1)
        self.assertIsNone(self.controller._client)

    @mock.patch("module.controller.CONNECT_TIMEOUT", new=0.2)
    def test_transport_404_during_cleanup_goes_logged_out_with_notice(self):
        self.with_credentials()
        self.client.connect_delay = 5
        self.client.session_stop_error = errors.AuthKeyUnregistered()
        fresh_client = FakeClient()
        factory_calls = []

        def factory():
            factory_calls.append(1)
            return self.client if len(factory_calls) == 1 else fresh_client

        self.controller._client_factory = factory
        self.controller.start()
        self.wait_for_state(State.LOGGED_OUT)
        self.assertEqual(self.controller.status()["notice"], "登录已失效，请重新登录")
        self.assertEqual(self.client.storage.delete_calls, 1)
        self.assertIs(self.controller._client, fresh_client)

    def test_factory_failure_goes_error(self):
        self.with_credentials()

        def bad_factory():
            raise RuntimeError("boom")

        self.controller._client_factory = bad_factory
        self.controller.start()
        self.wait_for_state(State.ERROR)
        self.assertIn("出错了", self.controller.status()["error"])
        self.assertIn("RuntimeError", self.controller.status()["error"])

    def test_terminate_failure_still_closes_session_and_storage(self):
        self.ready_controller()
        self.client.terminate_error = RuntimeError("terminate boom")
        self.controller._run(self.controller._cleanup_client(self.client))
        self.assertGreaterEqual(self.client.session.stop_calls, 1)
        self.assertGreaterEqual(self.client.storage.close_calls, 1)


def valid_form(tmp, **overrides):
    form = {
        "api_id": "12345",
        "api_hash": HASH,
        "proxy": None,
        "save_path": os.path.join(tmp, "dl"),
        "media_types": ["video", "photo"],
        "max_download_task": 3,
    }
    form.update(overrides)
    return form


class ConfigAndDownloadTestCase(ControllerTestBase):
    def ready_with_chat(self):
        self.ready_controller()
        self.client.dialogs = [SimpleNamespace(chat=make_chat(-1001, "News"))]
        self.controller.list_dialogs()
        self.controller.save_chats([-1001])

    def test_save_config_writes_file_and_connects(self):
        self.controller.start()
        self.client.authorized = True
        self.controller.save_config(valid_form(self.tmp))
        self.wait_for_state(State.READY)
        config = self.controller.get_config()
        self.assertEqual(config["api_id"], 12345)
        self.assertEqual(config["max_download_task"], 3)
        self.assertEqual(self.app.max_download_task, 3)

    def test_save_config_same_credentials_does_not_reconnect(self):
        self.controller.start()
        self.client.authorized = True
        self.controller.save_config(valid_form(self.tmp))
        self.wait_for_state(State.READY)
        connects = self.client.calls.count("connect")
        self.controller.save_config(
            valid_form(self.tmp, save_path=os.path.join(self.tmp, "other"))
        )
        self.assertIs(self.controller.state, State.READY)
        self.assertEqual(self.client.calls.count("connect"), connects)

    def test_save_config_proxy_change_reconnects(self):
        self.controller.start()
        self.client.authorized = True
        self.controller.save_config(valid_form(self.tmp))
        self.wait_for_state(State.READY)
        connects = self.client.calls.count("connect")
        proxy = {"scheme": "socks5", "hostname": "127.0.0.1", "port": 7890}
        self.controller.save_config(valid_form(self.tmp, proxy=proxy))
        self.wait_for_state(State.READY)
        self.assertEqual(self.client.calls.count("connect"), connects + 1)
        self.assertEqual(self.app.proxy["port"], 7890)

    def test_save_config_proxy_change_clears_resolved(self):
        self.controller.start()
        self.client.authorized = True
        self.controller.save_config(valid_form(self.tmp))
        self.wait_for_state(State.READY)
        self.client.chats["mychan"] = make_chat(-1005, "Mine", username="mychan")
        self.controller.resolve_chat("https://t.me/mychan")
        self.assertIn(-1005, self.controller._resolved)
        proxy = {"scheme": "socks5", "hostname": "127.0.0.1", "port": 7890}
        self.controller.save_config(valid_form(self.tmp, proxy=proxy))
        self.wait_for_state(State.READY)
        self.assertEqual(self.controller._resolved, {})

    def test_save_config_rejected_while_downloading(self):
        self.ready_with_chat()
        self.controller.start_download()
        with self.assertRaises(InvalidState):
            self.controller.save_config(valid_form(self.tmp))
        self.controller.stop_download()

    def test_list_dialogs_filters_types_and_caches(self):
        self.ready_controller()
        self.client.dialogs = [
            SimpleNamespace(chat=make_chat(-1001, "News", ChatType.CHANNEL, "news")),
            SimpleNamespace(chat=make_chat(42, "Bob", ChatType.PRIVATE)),
            SimpleNamespace(chat=make_chat(-1002, "Group", ChatType.SUPERGROUP)),
        ]
        dialogs = self.controller.list_dialogs()
        self.assertEqual([d["id"] for d in dialogs], [-1001, -1002])
        self.assertEqual(dialogs[0]["type"], "channel")
        self.controller.list_dialogs()
        self.assertEqual(self.client.calls.count("get_dialogs"), 1)
        self.controller.list_dialogs(refresh=True)
        self.assertEqual(self.client.calls.count("get_dialogs"), 2)

    def test_save_config_login_clears_startup_notice(self):
        self.controller.set_notice("配置文件损坏，已备份为 x 并恢复默认配置")
        self.controller.start()
        self.assertIs(self.controller.state, State.NEED_CONFIG)
        self.client.authorized = True
        self.controller.save_config(valid_form(self.tmp))
        self.wait_for_state(State.READY)
        self.assertEqual(self.controller.status()["notice"], "")

    def test_resolve_chat_and_save(self):
        # a chat known only from a pasted link is saved by username: its
        # numeric id needs the session's peer cache, gone after re-login
        self.ready_controller()
        self.client.chats["mychan"] = make_chat(-1005, "Mine", username="mychan")
        info = self.controller.resolve_chat("https://t.me/mychan")
        self.assertEqual(info["id"], -1005)
        self.controller.save_chats([-1005])
        expected = [{"chat_id": "mychan", "last_read_message_id": 0}]
        self.assertEqual(read_chats(self.config_path), expected)
        self.assertEqual(list(self.app.chat_download_config), ["mychan"])
        self.assertEqual(
            self.controller.current_chats(),
            [{"chat_id": "mychan", "dialog_id": -1005, "title": "Mine"}],
        )
        # saving again keeps the same single entry
        self.controller.save_chats([-1005])
        self.assertEqual(read_chats(self.config_path), expected)
        self.controller.save_chats([-1005, "mychan"])
        self.assertEqual(read_chats(self.config_path), expected)

    def test_joined_chat_is_saved_by_id_even_with_username(self):
        self.ready_controller()
        chat = make_chat(-1001, "News", username="news")
        self.client.dialogs = [SimpleNamespace(chat=chat)]
        self.client.chats["news"] = chat
        self.controller.list_dialogs()
        self.controller.resolve_chat("t.me/news")
        self.controller.save_chats([-1001])
        self.assertEqual(
            read_chats(self.config_path),
            [{"chat_id": -1001, "last_read_message_id": 0}],
        )

    def test_save_chats_drops_duplicates(self):
        self.ready_controller()
        self.client.dialogs = [
            SimpleNamespace(chat=make_chat(-1001, "A")),
            SimpleNamespace(chat=make_chat(-1002, "B")),
        ]
        self.controller.list_dialogs()
        self.controller.save_chats([-1002, -1001, "-1002", -1001, " @-1002 "])
        self.assertEqual(
            [c["chat_id"] for c in read_chats(self.config_path)], [-1002, -1001]
        )
        self.assertEqual(list(self.app.chat_download_config), [-1002, -1001])

    def test_resolve_unknown_chat(self):
        self.ready_controller()
        with self.assertRaises(GuiError) as ctx:
            self.controller.resolve_chat("t.me/nobody_here")
        self.assertIn("找不到", ctx.exception.message)
        self.assertIs(self.controller.state, State.READY)

    def test_save_chats_keeps_existing_progress(self):
        write_chats(
            self.config_path, [{"chat_id": "mychan", "last_read_message_id": 42}]
        )
        self.app.load_config()
        self.ready_controller()
        self.client.dialogs = [
            SimpleNamespace(chat=make_chat(-1001, "Mine", username="MyChan"))
        ]
        self.controller.list_dialogs()
        self.assertEqual(
            self.controller.current_chats(),
            [{"chat_id": "mychan", "dialog_id": -1001, "title": "Mine"}],
        )
        self.controller.save_chats([-1001])
        self.assertEqual(
            self.app.chat_download_config["mychan"].last_read_message_id, 42
        )

    def test_save_chats_rejects_bad_payload(self):
        self.ready_controller()
        for payload in (None, "abc", [1.5], [True]):
            with self.subTest(payload=payload):
                with self.assertRaises(GuiError):
                    self.controller.save_chats(payload)

    def test_start_requires_chats(self):
        self.ready_controller()
        with self.assertRaises(GuiError) as ctx:
            self.controller.start_download()
        self.assertIn("频道", ctx.exception.message)
        self.assertIs(self.controller.state, State.READY)

    def test_download_finishes_naturally(self):
        self.ready_with_chat()
        self.controller.start_download()
        self.assertIs(self.controller.state, State.DOWNLOADING)
        self.assertIs(self.downloader.started_with, self.client)
        self.loop.call_soon_threadsafe(self.downloader.finished.set)
        self.wait_for_state(State.READY)
        self.assertIn("下载完成", self.controller.status()["notice"])
        self.assertEqual(self.downloader.stop_calls, 1)

    def test_stop_download(self):
        self.ready_with_chat()
        self.controller.start_download()
        self.controller.stop_download()
        self.assertIs(self.controller.state, State.READY)
        self.assertIn("已停止", self.controller.status()["notice"])
        time.sleep(0.2)
        self.assertEqual(self.downloader.stop_calls, 1)

    def test_start_twice_is_rejected(self):
        self.ready_with_chat()
        self.controller.start_download()
        with self.assertRaises(InvalidState):
            self.controller.start_download()
        self.controller.stop_download()

    def test_shutdown_while_downloading_stops_and_disconnects(self):
        self.ready_with_chat()
        self.controller.start_download()
        self.controller.shutdown(timeout=5)
        self.assertEqual(self.downloader.stop_calls, 1)
        self.assertIn("disconnect", self.client.calls)

    @mock.patch("module.controller.STOP_WAIT", new=0.1)
    def test_stop_timeout_returns_409_then_finishes_later(self):
        self.ready_with_chat()
        self.controller.start_download()
        self.downloader.stop_delay = 1.0
        with self.assertRaises(GuiError) as ctx:
            self.controller.stop_download()
        self.assertEqual(ctx.exception.status, 409)
        self.assertIn("仍在停止", ctx.exception.message)
        self.assertIs(self.controller.state, State.STOPPING)
        with self.assertRaises(InvalidState):
            self.controller.start_download()
        self.wait_for_state(State.READY)

    def test_stop_error_surfaces_as_gui_error(self):
        self.ready_with_chat()
        self.controller.start_download()
        self.downloader.stop_error = RuntimeError("boom")
        with self.assertRaises(GuiError) as ctx:
            self.controller.stop_download()
        self.assertEqual(ctx.exception.status, 500)
        self.assertIn("停止下载时出错", ctx.exception.message)
        self.assertIs(self.controller.state, State.ERROR)
        # the failed stop never signalled "finished"; wake the watcher so it
        # doesn't sit pending until tearDown closes the loop
        self.loop.call_soon_threadsafe(self.downloader.finished.set)

    @mock.patch("module.controller.STOP_WAIT", new=0.1)
    def test_shutdown_waits_for_in_flight_stop_before_disconnect(self):
        self.ready_with_chat()
        self.controller.start_download()
        events = []
        orig_stop = self.downloader.stop_download
        orig_disconnect = self.client.disconnect

        async def tracked_stop():
            result = await orig_stop()
            events.append("stop")
            return result

        async def tracked_disconnect():
            events.append("disconnect")
            return await orig_disconnect()

        self.downloader.stop_download = tracked_stop
        self.client.disconnect = tracked_disconnect
        self.downloader.stop_delay = 0.3

        with self.assertRaises(GuiError):
            self.controller.stop_download()
        self.assertIs(self.controller.state, State.STOPPING)

        self.controller.shutdown(timeout=2)
        self.assertEqual(events, ["stop", "disconnect"])

    def test_shutdown_disconnects_even_if_stop_fails(self):
        self.ready_with_chat()
        self.controller.start_download()
        self.downloader.stop_error = RuntimeError("boom")
        self.controller.shutdown(timeout=2)
        self.assertIn("disconnect", self.client.calls)

    def test_shutdown_disconnects_even_if_stop_times_out(self):
        self.ready_with_chat()
        self.controller.start_download()
        self.downloader.stop_delay = 0.3
        self.controller.shutdown(timeout=0.05)
        self.assertIn("disconnect", self.client.calls)
        time.sleep(0.4)  # let the abandoned stop finish before tearDown stops the loop


class WrappedFakeClient(FakeClient):
    """Like the real HookClient: public coroutine methods go through pyrogram's
    async-to-sync wrapper, which runs them on a throwaway loop when they are
    called from a thread whose loop isn't running (e.g. a Flask request thread).
    """

    def __init__(self):
        super().__init__()
        self.loops = []

    async def send_code(self, phone):
        self.loops.append(asyncio.get_running_loop())
        return await FakeClient.send_code(self, phone)

    async def sign_in(self, phone, phone_code_hash, code):
        self.loops.append(asyncio.get_running_loop())
        return await FakeClient.sign_in(self, phone, phone_code_hash, code)

    async def get_password_hint(self):
        self.loops.append(asyncio.get_running_loop())
        return await FakeClient.get_password_hint(self)

    async def check_password(self, password):
        self.loops.append(asyncio.get_running_loop())
        return await FakeClient.check_password(self, password)

    async def log_out(self):
        self.loops.append(asyncio.get_running_loop())
        return await FakeClient.log_out(self)

    async def get_chat(self, username):
        self.loops.append(asyncio.get_running_loop())
        return await FakeClient.get_chat(self, username)


for _name in (
    "send_code",
    "sign_in",
    "get_password_hint",
    "check_password",
    "log_out",
    "get_chat",
):
    pyrogram_sync.async_to_sync(WrappedFakeClient, _name)


class RequestThreadTestCase(ControllerTestBase):
    """Controller methods are called from Flask threads; every Telegram call must
    still run on the controller's loop (regression: "attached to a different loop")."""

    def setUp(self):
        super().setUp()
        self.client = WrappedFakeClient()

    def call_in_thread(self, fn, *args):
        outcome = {}

        def target():
            try:
                outcome["result"] = fn(*args)
            except BaseException as e:  # re-raised in the test thread
                outcome["error"] = e

        thread = threading.Thread(target=target)
        thread.start()
        thread.join(10)
        if "error" in outcome:
            raise outcome["error"]
        return outcome.get("result")

    def test_login_calls_run_on_controller_loop(self):
        self.logged_out_controller()
        self.client.sign_in_result = errors.SessionPasswordNeeded()
        self.call_in_thread(self.controller.send_code, "+8613800000000")
        self.call_in_thread(self.controller.sign_in, "12345")
        self.call_in_thread(self.controller.check_password, "pw")
        self.assertIs(self.controller.state, State.READY)
        self.assertEqual(len(self.client.loops), 4)
        self.assertEqual({id(loop) for loop in self.client.loops}, {id(self.loop)})

    def test_resolve_and_log_out_run_on_controller_loop(self):
        self.ready_controller()
        self.client.chats["mychan"] = make_chat(-1005, "Mine", username="mychan")
        self.call_in_thread(self.controller.resolve_chat, "t.me/mychan")
        self.call_in_thread(self.controller.log_out)
        self.wait_for_state(State.LOGGED_OUT)
        self.assertEqual({id(loop) for loop in self.client.loops}, {id(self.loop)})
