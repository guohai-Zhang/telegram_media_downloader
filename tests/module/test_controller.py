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
from pyrogram.enums import ChatType

from module.app import Application
from module.controller import NETWORK_ERROR, Controller, State
from module.gui_config import GuiError, InvalidState, ensure_config_file, write_chats

HASH = "0123456789abcdef0123456789abcdef"


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

    async def connect(self):
        self.calls.append("connect")
        if self.connect_delay:
            await asyncio.sleep(self.connect_delay)
        if self.connect_error:
            raise self.connect_error
        self.is_connected = True
        return self.authorized

    async def disconnect(self):
        self.calls.append("disconnect")
        self.is_connected = False

    async def terminate(self):
        self.calls.append("terminate")
        self.is_initialized = False

    async def invoke(self, query):
        self.calls.append(type(query).__name__)

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

    async def start_download(self, client):
        self.started_with = client
        self.finished = asyncio.Event()

    async def wait_until_finished(self):
        await self.finished.wait()

    async def stop_download(self):
        self.stop_calls += 1
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
