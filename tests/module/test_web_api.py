"""test GUI web api"""

import json
import platform
import socket
import unittest

from module import download_stat, web
from module.gui_config import GuiError, InvalidState

TOKEN = "tok-123"


class FakeController:
    def __init__(self):
        self.received = []
        self.raise_on = {}

    def _call(self, name, *args):
        self.received.append((name,) + args)
        if name in self.raise_on:
            raise self.raise_on[name]
        return {"called": name}

    def status(self):
        return self._call("status")

    def get_config(self):
        return self._call("get_config")

    def save_config(self, form):
        self._call("save_config", form)

    def retry(self):
        self._call("retry")

    def send_code(self, phone):
        self._call("send_code", phone)

    def sign_in(self, code):
        self._call("sign_in", code)

    def check_password(self, password):
        self._call("check_password", password)

    def log_out(self):
        self._call("log_out")

    def list_dialogs(self, refresh=False):
        return self._call("list_dialogs", refresh)

    def resolve_chat(self, link):
        return self._call("resolve_chat", link)

    def current_chats(self):
        return self._call("current_chats")

    def save_chats(self, chat_ids):
        self._call("save_chats", chat_ids)

    def start_download(self):
        self._call("start_download")

    def stop_download(self):
        self._call("stop_download")


class FakeNative:
    def __init__(self):
        self.calls = []

    def choose_folder(self):
        self.calls.append("choose_folder")
        return "/tmp/chosen"

    def open_log_folder(self):
        self.calls.append("open_log_folder")


class WebApiTestCase(unittest.TestCase):
    def setUp(self):
        download_stat.reset_download_stat()
        self.login_disabled = web.get_flask_app().config.get("LOGIN_DISABLED", False)
        self.controller = FakeController()
        web.register_gui(self.controller, TOKEN)
        self.client = web.get_flask_app().test_client()
        self.headers = {"X-Token": TOKEN}

    def tearDown(self):
        web._gui["controller"] = None
        web._gui["token"] = ""
        web._gui["native"] = None
        web.get_flask_app().config["LOGIN_DISABLED"] = self.login_disabled
        download_stat.reset_download_stat()

    def test_api_requires_token(self):
        self.assertEqual(self.client.get("/api/status").status_code, 403)
        self.assertEqual(
            self.client.get("/api/status", headers={"X-Token": "bad"}).status_code, 403
        )
        res = self.client.get("/api/status", headers=self.headers)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(
            res.get_json(),
            {"ok": True, "data": {"called": "status", "paused": False}},
        )

    def test_status_reports_pause_state(self):
        download_stat.set_download_state(download_stat.DownloadState.StopDownload)
        res = self.client.get("/api/status", headers=self.headers)
        self.assertIs(res.get_json()["data"]["paused"], True)
        download_stat.set_download_state(download_stat.DownloadState.Downloading)
        res = self.client.get("/api/status", headers=self.headers)
        self.assertIs(res.get_json()["data"]["paused"], False)

    def test_foreign_host_is_forbidden_in_gui_mode(self):
        # DNS rebinding: a page on evil.example re-resolved to 127.0.0.1
        evil = {"Host": "evil.example:5000", "X-Token": TOKEN}
        for path in ("/", "/get_download_list?already_down=false", "/api/status"):
            with self.subTest(path=path):
                res = self.client.get(path, headers=evil)
                self.assertEqual(res.status_code, 403)
                self.assertEqual(res.get_json(), {"ok": False, "error": "forbidden"})
        res = self.client.post("/api/download/start", headers=evil)
        self.assertEqual(res.status_code, 403)
        self.assertNotIn(("start_download",), self.controller.received)

    def test_local_hosts_are_allowed_in_gui_mode(self):
        self.assertEqual(self.client.get("/").status_code, 200)
        for host in ("127.0.0.1:5000", "localhost:5123", "LOCALHOST", "127.0.0.1"):
            with self.subTest(host=host):
                headers = dict(self.headers, Host=host)
                res = self.client.get("/api/status", headers=headers)
                self.assertEqual(res.status_code, 200)

    def test_host_is_not_checked_in_cli_mode(self):
        web._gui["controller"] = None
        res = self.client.get(
            "/get_download_list?already_down=false",
            headers={"Host": "evil.example:5000"},
        )
        self.assertEqual(res.status_code, 200)

    def test_non_ascii_token_is_forbidden_not_500(self):
        res = self.client.get("/api/status", headers={"X-Token": "tök"})
        self.assertEqual(res.status_code, 403)

    def test_existing_post_routes_require_token_in_gui_mode(self):
        res = self.client.post("/set_download_state?state=pause")
        self.assertEqual(res.status_code, 403)
        res = self.client.post("/set_download_state?state=pause", headers=self.headers)
        self.assertEqual(res.status_code, 200)

    def test_api_is_404_without_gui(self):
        web._gui["controller"] = None
        self.assertEqual(
            self.client.get("/api/status", headers=self.headers).status_code, 404
        )

    def test_errors_are_json(self):
        cases = [
            (GuiError("坏了"), 400, "坏了"),
            (InvalidState(), 409, "当前状态下不能执行这个操作"),
            (RuntimeError("boom"), 500, "出错了：RuntimeError，详情见日志"),
        ]
        for error, status, message in cases:
            with self.subTest(error=error):
                self.controller.raise_on["start_download"] = error
                res = self.client.post("/api/download/start", headers=self.headers)
                self.assertEqual(res.status_code, status)
                self.assertEqual(res.get_json(), {"ok": False, "error": message})

    def test_routes_forward_arguments(self):
        h = self.headers
        self.client.post("/api/config", json={"api_id": "1"}, headers=h)
        self.client.post("/api/retry", headers=h)
        self.client.post("/api/login/phone", json={"phone": "+1"}, headers=h)
        self.client.post("/api/login/code", json={"code": "123"}, headers=h)
        self.client.post("/api/login/password", json={"password": "pw"}, headers=h)
        self.client.post("/api/logout", headers=h)
        self.client.get("/api/dialogs?refresh=1", headers=h)
        self.client.get("/api/dialogs", headers=h)
        self.client.post("/api/chats/resolve", json={"link": "t.me/x"}, headers=h)
        self.client.get("/api/chats", headers=h)
        self.client.post("/api/chats", json={"chat_ids": [1, "a"]}, headers=h)
        self.client.post("/api/download/start", headers=h)
        self.client.post("/api/download/stop", headers=h)
        self.client.get("/api/config", headers=h)
        self.assertEqual(
            self.controller.received,
            [
                ("save_config", {"api_id": "1"}),
                ("retry",),
                ("send_code", "+1"),
                ("sign_in", "123"),
                ("check_password", "pw"),
                ("log_out",),
                ("list_dialogs", True),
                ("list_dialogs", False),
                ("resolve_chat", "t.me/x"),
                ("current_chats",),
                ("save_chats", [1, "a"]),
                ("start_download",),
                ("stop_download",),
                ("get_config",),
            ],
        )

    def test_non_object_json_body_is_treated_as_empty(self):
        self.client.post(
            "/api/login/phone",
            data="[1]",
            headers=dict(self.headers, **{"Content-Type": "application/json"}),
        )
        self.assertEqual(self.controller.received, [("send_code", "")])

    def test_download_list_is_valid_json_with_hostile_names(self):
        name = '/tmp/x/a"b<img src=x onerror=alert(1)>.mp4'
        download_stat.get_download_result()[-100] = {
            7: {
                "down_byte": 50,
                "total_size": 100,
                "file_name": name,
                "download_speed": 10,
            },
            8: {
                "down_byte": 0,
                "total_size": 0,
                "file_name": "/tmp/empty.bin",
                "download_speed": 0,
            },
        }
        res = self.client.get("/get_download_list?already_down=false")
        rows = json.loads(res.data)
        self.assertEqual(rows[0]["filename"], 'a"b<img src=x onerror=alert(1)>.mp4')
        self.assertEqual(rows[0]["download_progress"], "50.0")
        self.assertEqual(rows[1]["download_progress"], "0")

    @unittest.skipIf(platform.system() == "Windows", "SO_REUSEADDR differs on Windows")
    def test_make_web_server_falls_back_when_port_taken(self):
        blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        blocker.bind(("127.0.0.1", 0))
        blocker.listen(1)
        taken = blocker.getsockname()[1]
        try:
            server, port = web.make_web_server("127.0.0.1", taken)
            try:
                self.assertNotEqual(port, taken)
                self.assertGreater(port, 0)
            finally:
                server.server_close()
        finally:
            blocker.close()

    def test_index_renders_gui_mode_flag(self):
        res = self.client.get("/")
        self.assertIn(b"static/gui/gui.js", res.data)
        web._gui["controller"] = None
        web.get_flask_app().config["LOGIN_DISABLED"] = True
        res = self.client.get("/")
        self.assertNotIn(b"static/gui/gui.js", res.data)

    def test_index_renders_native_mode_flag(self):
        res = self.client.get("/")
        self.assertIn(b'data-native="0"', res.data)
        web.register_gui(self.controller, TOKEN, native=FakeNative())
        res = self.client.get("/")
        self.assertIn(b'data-native="1"', res.data)

    def test_native_routes_404_without_native(self):
        for path in ("choose_folder", "open_log_folder"):
            res = self.client.post(f"/api/native/{path}", headers=self.headers)
            self.assertEqual(res.status_code, 404)
            self.assertEqual(res.get_json(), {"ok": False, "error": "仅在应用窗口中可用"})

    def test_native_routes_forward_to_native_object(self):
        native = FakeNative()
        web.register_gui(self.controller, TOKEN, native=native)
        res = self.client.post("/api/native/choose_folder", headers=self.headers)
        self.assertEqual(res.get_json(), {"ok": True, "data": "/tmp/chosen"})
        res = self.client.post("/api/native/open_log_folder", headers=self.headers)
        self.assertEqual(res.get_json(), {"ok": True, "data": None})
        self.assertEqual(native.calls, ["choose_folder", "open_log_folder"])
