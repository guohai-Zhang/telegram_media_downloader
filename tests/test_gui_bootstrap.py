"""test gui_main bootstrap helpers (no window is opened)"""

import os
import platform
import shutil
import sys
import tempfile
import unittest
from unittest import mock

from loguru import logger

import gui_main


class GuiBootstrapTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_paths(self):
        self.assertEqual(
            gui_main.data_dir(self.tmp),
            os.path.join(
                self.tmp, "Library", "Application Support", "TelegramMediaDownloader"
            ),
        )
        self.assertEqual(
            gui_main.default_save_path(self.tmp),
            os.path.join(self.tmp, "Downloads", "Telegram"),
        )

    def test_home_override_from_environment(self):
        with mock.patch.dict(os.environ, {"TDL_GUI_HOME": self.tmp}):
            self.assertTrue(gui_main.data_dir().startswith(self.tmp))

    @unittest.skipIf(platform.system() == "Windows", "fcntl is POSIX only")
    def test_single_instance_lock(self):
        first = gui_main.acquire_single_instance_lock(self.tmp)
        self.assertIsNotNone(first)
        self.assertIsNone(gui_main.acquire_single_instance_lock(self.tmp))
        first.close()
        again = gui_main.acquire_single_instance_lock(self.tmp)
        self.assertIsNotNone(again)
        again.close()

    def test_ensure_std_streams_replaces_missing_streams(self):
        with mock.patch.object(sys, "stdout", None), mock.patch.object(
            sys, "stderr", None
        ):
            gui_main.ensure_std_streams()
            self.assertIsNotNone(sys.stdout)
            self.assertIsNotNone(sys.stderr)
            sys.stdout.write("x")
            sys.stdout.close()
            sys.stderr.close()

    def test_alert_passes_text_as_arguments(self):
        message = '启动失败：X，日志在 /a "b" \\ $(c)'
        with mock.patch.object(gui_main.subprocess, "run") as run:
            gui_main._alert(message)
        command = run.call_args[0][0]
        self.assertEqual(command[0], "osascript")
        self.assertEqual(command[-2:], [gui_main.WINDOW_TITLE, message])
        script = " ".join(command[1:-2])
        self.assertIn("on run argv", script)
        self.assertNotIn("$(c)", script)

    @unittest.skipIf(platform.system() == "Windows", "fcntl is POSIX only")
    def test_startup_failure_is_logged_and_alerted(self):
        sink_ids = []
        real_add = gui_main._add_log_file

        def add_log_file(log_dir, level):
            sink_ids.append(real_add(log_dir, level))
            return sink_ids[-1]

        cwd = os.getcwd()
        try:
            with mock.patch.dict(
                os.environ, {"TDL_GUI_HOME": self.tmp}
            ), mock.patch.object(
                gui_main, "_add_log_file", side_effect=add_log_file
            ), mock.patch.object(
                gui_main, "_run", side_effect=AttributeError("boom")
            ), mock.patch.object(
                gui_main, "_alert"
            ) as alert:
                self.assertEqual(gui_main.main(), 1)
        finally:
            os.chdir(cwd)
            for sink_id in sink_ids:
                logger.remove(sink_id)

        directory = gui_main.data_dir(self.tmp)
        log_dir = os.path.join(directory, "log")
        alert.assert_called_once_with(f"启动失败：AttributeError，日志在 {log_dir}")
        with open(os.path.join(log_dir, "tdl.log"), encoding="utf-8") as f:
            content = f.read()
        self.assertIn("Traceback", content)
        self.assertIn("AttributeError: boom", content)
        # the single-instance lock was released
        lock = gui_main.acquire_single_instance_lock(directory)
        self.assertIsNotNone(lock)
        lock.close()
