"""test gui_config"""

import os
import shutil
import tempfile
import unittest
from unittest import mock

from ruamel import yaml

from module import gui_config
from module.app import DEFAULT_MEDIA_TYPES, Application
from module.gui_config import (
    GuiError,
    InvalidState,
    ensure_config_file,
    ensure_data_file,
    find_chat,
    merge_chats,
    normalize_phone,
    parse_chat_link,
    read_basic_config,
    read_chats,
    validate_basic_config,
    write_basic_config,
    write_chats,
)

HASH = "0123456789abcdef0123456789abcdef"


def load_yaml(path):
    with open(path, encoding="utf-8") as f:
        return yaml.YAML().load(f)


class GuiConfigTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.config_path = os.path.join(self.tmp, "config.yaml")
        self.save_path = os.path.join(self.tmp, "downloads")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def form(self, **overrides):
        form = {
            "api_id": "12345",
            "api_hash": HASH,
            "proxy": None,
            "save_path": self.save_path,
            "media_types": ["photo", "video"],
            "max_download_task": 5,
        }
        form.update(overrides)
        return form

    # ---- errors
    def test_error_types(self):
        self.assertEqual(GuiError("x").status, 400)
        self.assertEqual(InvalidState().status, 409)
        self.assertEqual(GuiError("boom", 500).message, "boom")

    # ---- ensure_config_file
    def test_ensure_config_file_creates_defaults(self):
        self.assertIsNone(ensure_config_file(self.config_path, self.save_path))
        data = load_yaml(self.config_path)
        self.assertEqual(data["api_id"], "")
        self.assertEqual(data["chat"], [])
        self.assertEqual(list(data["media_types"]), DEFAULT_MEDIA_TYPES)
        self.assertEqual(data["save_path"], self.save_path)

    def test_ensure_config_file_keeps_valid_file(self):
        with open(self.config_path, "w", encoding="utf-8") as f:
            f.write("# mine\napi_id: 1\n")
        self.assertIsNone(ensure_config_file(self.config_path, self.save_path))
        with open(self.config_path, encoding="utf-8") as f:
            self.assertEqual(f.read(), "# mine\napi_id: 1\n")

    def test_ensure_config_file_backs_up_broken_file(self):
        with open(self.config_path, "w", encoding="utf-8") as f:
            f.write("api_id: [unclosed\n")
        notice = ensure_config_file(self.config_path, self.save_path)
        self.assertIn("已备份", notice)
        backups = [n for n in os.listdir(self.tmp) if ".broken-" in n]
        self.assertEqual(len(backups), 1)
        with open(os.path.join(self.tmp, backups[0]), encoding="utf-8") as f:
            self.assertEqual(f.read(), "api_id: [unclosed\n")
        self.assertEqual(load_yaml(self.config_path)["api_id"], "")

    def test_ensure_config_file_rewrites_empty_file_without_backup(self):
        open(self.config_path, "w", encoding="utf-8").close()
        self.assertIsNone(ensure_config_file(self.config_path, self.save_path))
        self.assertEqual(load_yaml(self.config_path)["api_id"], "")
        self.assertFalse([n for n in os.listdir(self.tmp) if ".broken-" in n])

    # ---- ensure_data_file
    def backups(self):
        return [n for n in os.listdir(self.tmp) if ".broken-" in n]

    def test_ensure_data_file_missing_is_fine(self):
        self.assertIsNone(ensure_data_file(os.path.join(self.tmp, "data.yaml")))
        self.assertEqual(os.listdir(self.tmp), [])

    def test_ensure_data_file_keeps_valid_file(self):
        path = os.path.join(self.tmp, "data.yaml")
        text = "chat:\n- chat_id: -1001\n  ids_to_retry: [3, 4]\n"
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        self.assertIsNone(ensure_data_file(path))
        with open(path, encoding="utf-8") as f:
            self.assertEqual(f.read(), text)

    def test_ensure_data_file_backs_up_and_removes_truncated_file(self):
        path = os.path.join(self.tmp, "data.yaml")
        text = "chat:\n- chat_id: -1001\n  ids_to_retry: [3, 4"
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        notice = ensure_data_file(path)
        self.assertIn("下载记录文件损坏", notice)
        self.assertIn("并重置", notice)
        self.assertFalse(os.path.exists(path))
        self.assertEqual(len(self.backups()), 1)
        self.assertTrue(self.backups()[0].startswith("data.yaml.broken-"))
        self.assertIn(self.backups()[0], notice)
        with open(os.path.join(self.tmp, self.backups()[0]), encoding="utf-8") as f:
            self.assertEqual(f.read(), text)

    def test_ensure_data_file_resets_non_mapping(self):
        path = os.path.join(self.tmp, "data.yaml")
        with open(path, "w", encoding="utf-8") as f:
            f.write("- 1\n- 2\n")
        self.assertIn("已备份", ensure_data_file(path))
        self.assertFalse(os.path.exists(path))
        self.assertEqual(len(self.backups()), 1)

    def test_ensure_data_file_removes_empty_file_without_backup(self):
        path = os.path.join(self.tmp, "data.yaml")
        open(path, "w", encoding="utf-8").close()
        self.assertIsNone(ensure_data_file(path))
        self.assertFalse(os.path.exists(path))
        self.assertEqual(self.backups(), [])

    def test_app_starts_clean_after_data_file_reset(self):
        ensure_config_file(self.config_path, self.save_path)
        write_chats(self.config_path, [{"chat_id": -1001, "last_read_message_id": 5}])
        data_path = os.path.join(self.tmp, "data.yaml")
        with open(data_path, "w", encoding="utf-8") as f:
            f.write("chat:\n- chat_id: -1001\n  ids_to_retry: [3, 4")
        ensure_data_file(data_path)
        app = Application(self.config_path, data_path)
        app.load_config()
        self.assertEqual(app.chat_download_config[-1001].ids_to_retry, [])

    # ---- thread safety
    def test_yaml_instance_is_used_under_the_lock(self):
        ensure_config_file(self.config_path, self.save_path)
        seen = []
        real_load, real_dump = gui_config._yaml.load, gui_config._yaml.dump

        def load(stream):
            seen.append(("load", gui_config._yaml_lock.locked()))
            return real_load(stream)

        def dump(data, stream):
            seen.append(("dump", gui_config._yaml_lock.locked()))
            return real_dump(data, stream)

        with mock.patch.object(gui_config._yaml, "load", side_effect=load):
            with mock.patch.object(gui_config._yaml, "dump", side_effect=dump):
                write_chats(self.config_path, [{"chat_id": 1}])
                read_chats(self.config_path)
        self.assertEqual(seen, [("load", True), ("dump", True), ("load", True)])
        self.assertFalse(gui_config._yaml_lock.locked())

    # ---- validate_basic_config
    def test_validate_normalizes_values(self):
        basic = validate_basic_config(
            self.form(
                api_id=" 12345 ", api_hash=HASH.upper(), media_types=["video", "photo"]
            )
        )
        self.assertEqual(basic["api_id"], 12345)
        self.assertEqual(basic["api_hash"], HASH)
        # stored in the canonical DEFAULT_MEDIA_TYPES order
        self.assertEqual(basic["media_types"], ["photo", "video"])
        self.assertTrue(os.path.isdir(self.save_path))
        self.assertIsNone(basic["proxy"])

    def test_validate_rejects_bad_values(self):
        cases = [
            ({"api_id": "abc"}, "api_id"),
            ({"api_id": "0"}, "api_id"),
            ({"api_hash": "short"}, "api_hash"),
            ({"media_types": []}, "媒体类型"),
            ({"media_types": ["video", "sticker"]}, "媒体类型"),
            ({"max_download_task": 0}, "同时下载文件数"),
            ({"max_download_task": 11}, "同时下载文件数"),
            ({"max_download_task": "x"}, "同时下载文件数"),
            ({"save_path": "  "}, "保存目录"),
            ({"proxy": {"scheme": "ftp", "hostname": "h", "port": 1}}, "代理类型"),
            ({"proxy": {"scheme": "socks5", "hostname": "", "port": 1}}, "代理地址"),
            ({"proxy": {"scheme": "socks5", "hostname": "h", "port": 70000}}, "代理端口"),
        ]
        for overrides, fragment in cases:
            with self.subTest(overrides=overrides):
                with self.assertRaises(GuiError) as ctx:
                    validate_basic_config(self.form(**overrides))
                self.assertIn(fragment, ctx.exception.message)

    def test_validate_proxy_drops_empty_credentials(self):
        proxy = {"scheme": "SOCKS5", "hostname": " 127.0.0.1 ", "port": "7890"}
        basic = validate_basic_config(
            self.form(proxy=dict(proxy, username="", password="secret"))
        )
        self.assertEqual(
            basic["proxy"], {"scheme": "socks5", "hostname": "127.0.0.1", "port": 7890}
        )
        basic = validate_basic_config(
            self.form(proxy=dict(proxy, username="u", password="p"))
        )
        self.assertEqual(basic["proxy"]["username"], "u")
        self.assertEqual(basic["proxy"]["password"], "p")

    def test_validate_rejects_unwritable_save_path(self):
        with mock.patch("module.gui_config.os.access", return_value=False):
            with self.assertRaises(GuiError) as ctx:
                validate_basic_config(self.form())
        self.assertIn("写入权限", ctx.exception.message)

    def test_save_path_with_tilde_spaces_and_cjk(self):
        with mock.patch.dict(os.environ, {"HOME": self.tmp, "USERPROFILE": self.tmp}):
            basic = validate_basic_config(self.form(save_path="~/电报 下载"))
        expected = os.path.join(self.tmp, "电报 下载")
        self.assertEqual(basic["save_path"], expected)
        self.assertTrue(os.path.isdir(expected))
        ensure_config_file(self.config_path, self.save_path)
        write_basic_config(self.config_path, basic)
        self.assertEqual(load_yaml(self.config_path)["save_path"], expected)

    # ---- write/read basic config
    def test_write_basic_config_preserves_comments_and_advanced_keys(self):
        with open(self.config_path, "w", encoding="utf-8") as f:
            f.write(
                "# my settings\n"
                "api_id: 1\n"
                "proxy:\n  scheme: http\n  hostname: old\n  port: 1\n"
                "max_concurrent_transmissions: 3\n"
                "chat:\n- chat_id: 9\n  download_filter: message_date >= 2024-01-01\n"
            )
        write_basic_config(self.config_path, validate_basic_config(self.form()))
        with open(self.config_path, encoding="utf-8") as f:
            text = f.read()
        self.assertIn("# my settings", text)
        self.assertIn("download_filter: message_date >= 2024-01-01", text)
        data = load_yaml(self.config_path)
        self.assertEqual(data["api_id"], 12345)
        self.assertNotIn("proxy", data)
        self.assertEqual(data["max_concurrent_transmissions"], 3)
        self.assertEqual(data["max_download_task"], 5)

    def test_write_basic_config_does_not_add_concurrent_transmissions(self):
        ensure_config_file(self.config_path, self.save_path)
        write_basic_config(self.config_path, validate_basic_config(self.form()))
        self.assertNotIn("max_concurrent_transmissions", load_yaml(self.config_path))

    def test_read_basic_config_from_application(self):
        ensure_config_file(self.config_path, self.save_path)
        write_basic_config(
            self.config_path,
            validate_basic_config(
                self.form(proxy={"scheme": "http", "hostname": "h", "port": 8080})
            ),
        )
        app = Application(self.config_path, os.path.join(self.tmp, "data.yaml"))
        app.load_config()
        self.assertEqual(
            read_basic_config(app),
            {
                "api_id": 12345,
                "api_hash": HASH,
                "proxy": {"scheme": "http", "hostname": "h", "port": 8080},
                "save_path": self.save_path,
                "media_types": ["photo", "video"],
                "max_download_task": 5,
            },
        )

    # ---- chats
    def test_merge_chats_keeps_existing_entries_in_new_order(self):
        existing = [
            {"chat_id": "mychan", "last_read_message_id": 42, "download_filter": "x"},
            {"chat_id": -1002, "last_read_message_id": 7},
            {"chat_id": -1003, "last_read_message_id": 1},
        ]
        merged = merge_chats(existing, [-1002, -1001, -1004], {-1001: "MyChan"})
        self.assertEqual(
            merged,
            [
                {"chat_id": -1002, "last_read_message_id": 7},
                {
                    "chat_id": "mychan",
                    "last_read_message_id": 42,
                    "download_filter": "x",
                },
                {"chat_id": -1004, "last_read_message_id": 0},
            ],
        )

    def test_read_and_write_chats(self):
        ensure_config_file(self.config_path, self.save_path)
        write_chats(self.config_path, [{"chat_id": 5, "last_read_message_id": 0}])
        self.assertEqual(read_chats(self.config_path)[0]["chat_id"], 5)

    def test_read_chats_understands_legacy_format(self):
        with open(self.config_path, "w", encoding="utf-8") as f:
            f.write("chat_id: oldchan\nlast_read_message_id: 30\n")
        self.assertEqual(
            read_chats(self.config_path),
            [{"chat_id": "oldchan", "last_read_message_id": 30}],
        )
        write_chats(self.config_path, read_chats(self.config_path))
        data = load_yaml(self.config_path)
        self.assertNotIn("chat_id", data)
        self.assertNotIn("last_read_message_id", data)
        self.assertEqual(data["chat"][0]["last_read_message_id"], 30)

    def test_find_chat(self):
        chats = [
            {"id": -1001, "title": "A", "username": "AChan"},
            {"id": -1002, "title": "B", "username": ""},
        ]
        self.assertEqual(find_chat(chats, -1002)["title"], "B")
        self.assertEqual(find_chat(chats, "@achan")["title"], "A")
        self.assertIsNone(find_chat(chats, "nobody"))

    # ---- parsing
    def test_parse_chat_link(self):
        ok = {
            "https://t.me/MyChannel": "MyChannel",
            "http://t.me/MyChannel/123": "MyChannel",
            "t.me/s/MyChannel": "MyChannel",
            "telegram.me/MyChannel?start=1": "MyChannel",
            "@MyChannel": "MyChannel",
            " MyChannel ": "MyChannel",
        }
        for text, expected in ok.items():
            with self.subTest(text=text):
                self.assertEqual(parse_chat_link(text), expected)
        for text in ("https://t.me/+AbCdEf", "t.me/joinchat/AbCdEf"):
            with self.subTest(text=text):
                with self.assertRaises(GuiError) as ctx:
                    parse_chat_link(text)
                self.assertIn("私有邀请链接", ctx.exception.message)
        for text in ("", "https://example.com/x", "ab", "1abc"):
            with self.subTest(text=text):
                with self.assertRaises(GuiError):
                    parse_chat_link(text)

    def test_normalize_phone(self):
        self.assertEqual(normalize_phone("+86 138-0000-0000"), "+8613800000000")
        self.assertEqual(normalize_phone("(1) 555 0100 200"), "15550100200")
        for text in ("", "abc", "+86", "12345678901234567"):
            with self.subTest(text=text):
                with self.assertRaises(GuiError):
                    normalize_phone(text)
