"""test download_stat"""

import unittest

from module import download_stat
from module.download_stat import DownloadState


class DownloadStatTestCase(unittest.TestCase):
    def test_reset_download_stat(self):
        download_stat.get_download_result()[1] = {2: {"down_byte": 1}}
        download_stat.set_download_state(DownloadState.StopDownload)
        result_ref = download_stat.get_download_result()

        download_stat.reset_download_stat()

        self.assertEqual(download_stat.get_download_result(), {})
        # web.py keeps using the same dict object, so it must be cleared in place
        self.assertIs(download_stat.get_download_result(), result_ref)
        self.assertEqual(download_stat.get_total_download_speed(), 0)
        self.assertIs(download_stat.get_download_state(), DownloadState.Downloading)
