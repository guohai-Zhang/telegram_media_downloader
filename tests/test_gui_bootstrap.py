"""test gui_main bootstrap helpers (no window is opened)"""

import os
import platform
import shutil
import sys
import tempfile
import unittest
from unittest import mock

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
