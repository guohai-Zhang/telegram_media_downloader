"""test app"""

import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

from ruamel import yaml

import module.app
from module.app import Application, ChatDownloadConfig, DownloadStatus, TaskNode
from module.gui_config import merge_chats, read_chats, write_chats

sys.path.append("..")  # Adds higher directory to python modules path.


class AppTestCase(unittest.TestCase):
    @classmethod
    def tearDownClass(cls):
        config_test = os.path.join(os.path.abspath("."), "config_test.yaml")
        data_test = os.path.join(os.path.abspath("."), "data_test.yaml")
        if os.path.exists(config_test):
            os.remove(config_test)
        if os.path.exists(data_test):
            os.remove(data_test)

    def test_app(self):
        app = Application("", "")
        self.assertEqual(app.save_path, os.path.join(os.path.abspath("."), "downloads"))
        self.assertEqual(app.proxy, {})
        self.assertEqual(app.restart_program, False)

        app.chat_download_config[123] = ChatDownloadConfig()
        app.chat_download_config[123].last_read_message_id = 13
        app.chat_download_config[123].node.download_status[
            6
        ] = DownloadStatus.Downloading
        app.chat_download_config[123].ids_to_retry.append(7)
        # download success
        app.chat_download_config[123].node.download_status[
            8
        ] = DownloadStatus.SuccessDownload
        app.chat_download_config[123].finish_task += 1
        # download success
        app.chat_download_config[123].node.download_status[
            10
        ] = DownloadStatus.SuccessDownload
        app.chat_download_config[123].finish_task += 1
        # not exist message
        app.chat_download_config[123].node.download_status[
            13
        ] = DownloadStatus.SuccessDownload
        app.config["chat"] = [{"chat_id": 123, "last_read_message_id": 5}]

        app.update_config(False)

        self.assertEqual(
            app.chat_download_config[123].last_read_message_id + 1,
            app.config["chat"][0]["last_read_message_id"],
        )
        self.assertEqual(
            [6, 7],
            app.app_data["chat"][0]["ids_to_retry"],
        )

    @mock.patch("module.app.os.replace")
    @mock.patch("__main__.__builtins__.open", new_callable=mock.mock_open)
    @mock.patch("module.app._yaml")
    def test_update_config(self, mock_yaml, mock_open, mock_replace):
        app = Application("", "")
        app.config_file = "config_test.yaml"
        app.app_data_file = "data_test.yaml"
        app.config["chat"] = [{"chat_id": 123, "last_read_message_id": 0}]
        app.update_config()
        # written through a temp file, then renamed over the real one
        mock_open.assert_called_with("data_test.yaml.tmp", "w", encoding="utf-8")
        mock_replace.assert_called_with("data_test.yaml.tmp", "data_test.yaml")

    def test_assign_config_tolerates_missing_keys(self):
        app = Application("", "")
        app.assign_config({})
        self.assertEqual(app.api_id, "")
        self.assertEqual(app.api_hash, "")
        self.assertEqual(app.media_types, module.app.DEFAULT_MEDIA_TYPES)
        self.assertEqual(app.file_formats, module.app.DEFAULT_FILE_FORMATS)
        self.assertEqual(app.chat_download_config, {})

    def test_assign_config_reload_drops_removed_chats(self):
        app = Application("", "")
        app.assign_config({"chat": [{"chat_id": 1}, {"chat_id": 2}]})
        app.assign_config({"chat": [{"chat_id": 2, "last_read_message_id": 9}]})
        self.assertEqual(list(app.chat_download_config), [2])
        self.assertEqual(app.chat_download_config[2].last_read_message_id, 9)

    def test_assign_config_reload_clears_proxy(self):
        app = Application("", "")
        app.assign_config({"proxy": {"scheme": "socks5", "hostname": "h", "port": 1}})
        self.assertEqual(app.proxy["port"], 1)
        app.assign_config({})
        self.assertEqual(app.proxy, {})


class UpdateConfigFilesTestCase(unittest.TestCase):
    """update_config against real config.yaml / data.yaml files"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.config_path = os.path.join(self.tmp, "config.yaml")
        self.data_path = os.path.join(self.tmp, "data.yaml")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, path, text):
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)

    def load(self, path):
        with open(path, encoding="utf-8") as f:
            return yaml.YAML().load(f)

    def test_removed_chat_does_not_shadow_retry_ids(self):
        self.write(
            self.config_path,
            "chat:\n"
            "- chat_id: -1001\n  last_read_message_id: 10\n"
            "- chat_id: -1002\n  last_read_message_id: 50\n",
        )
        self.write(
            self.data_path,
            "chat:\n"
            "- chat_id: -1001\n  ids_to_retry: [3]\n"
            "- chat_id: -1002\n  ids_to_retry: [40, 41]\n",
        )
        app = Application(self.config_path, self.data_path)
        app.load_config()

        # what Controller.save_chats does when -1001 is unticked
        chats = merge_chats(read_chats(self.config_path), [-1002], {})
        write_chats(self.config_path, chats)
        app.load_config()
        self.assertEqual(list(app.chat_download_config), [-1002])
        self.assertEqual(app.chat_download_config[-1002].ids_to_retry, [40, 41])

        # a run: 40, 41 and 60 finish, 55 is still in flight when Stop is hit
        node = TaskNode(chat_id=-1002)
        app.chat_download_config[-1002].node = node
        for message_id in (40, 41, 60):
            node.download_status[message_id] = DownloadStatus.SuccessDownload
            app.set_download_id(node, message_id, DownloadStatus.SuccessDownload)
        node.download_status[55] = DownloadStatus.Downloading
        app.update_config()

        data = self.load(self.data_path)
        self.assertEqual(len(data["chat"]), 1)
        self.assertEqual(data["chat"][0]["chat_id"], -1002)
        self.assertEqual(list(data["chat"][0]["ids_to_retry"]), [55])

        again = Application(self.config_path, self.data_path)
        again.load_config()
        self.assertEqual(again.chat_download_config[-1002].ids_to_retry, [55])
        self.assertEqual(again.chat_download_config[-1002].last_read_message_id, 61)

    def test_update_config_leaves_no_temp_files(self):
        self.write(self.config_path, "chat:\n- chat_id: 5\n")
        app = Application(self.config_path, self.data_path)
        app.load_config()
        app.update_config()
        self.assertEqual(sorted(os.listdir(self.tmp)), ["config.yaml", "data.yaml"])
        self.assertEqual(self.load(self.data_path)["chat"][0]["chat_id"], 5)
