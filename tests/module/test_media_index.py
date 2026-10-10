"""Unittest module for the duplicate-media index."""
import json
import os
import tempfile
import unittest

from module.media_index import MediaIndex, MediaInfo

MB = 1024 * 1024


def video(uid=None, size=50 * MB, duration=120, ext=".mp4"):
    return MediaInfo(unique_id=uid, size=size, duration=duration, ext=ext)


class MediaIndexTestCase(unittest.TestCase):
    def setUp(self):
        self.index = MediaIndex(None, min_size=MB)

    def test_same_unique_id_is_duplicate(self):
        self.index.add("c", 1, video(uid="A", size=10))
        self.assertIsNotNone(self.index.find("c", 2, video(uid="A", size=20)))

    def test_same_size_and_duration_is_duplicate(self):
        self.index.add("c", 1, video(uid="A"))
        self.assertIsNotNone(self.index.find("c", 2, video(uid="B")))

    def test_same_size_different_duration_is_not_duplicate(self):
        self.index.add("c", 1, video(uid="A", duration=120))
        self.assertIsNone(self.index.find("c", 2, video(uid="B", duration=121)))

    def test_same_size_different_extension_is_not_duplicate(self):
        self.index.add("c", 1, video(uid="A", ext=".mp4"))
        self.assertIsNone(self.index.find("c", 2, video(uid="B", ext=".mkv")))

    def test_small_files_only_match_by_unique_id(self):
        self.index.add("c", 1, video(uid="A", size=MB - 1))
        self.assertIsNone(self.index.find("c", 2, video(uid="B", size=MB - 1)))

    def test_other_chat_is_not_duplicate(self):
        self.index.add("c1", 1, video(uid="A"))
        self.assertIsNone(self.index.find("c2", 2, video(uid="A")))

    def test_message_never_duplicates_itself(self):
        self.index.add("c", 1, video(uid="A"))
        self.assertIsNone(self.index.find("c", 1, video(uid="A")))

    def test_reserved_download_blocks_a_concurrent_copy(self):
        self.index.reserve("c", 1, video(uid="A"))
        self.assertIsNotNone(self.index.find("c", 2, video(uid="A")))

    def test_released_reservation_is_forgotten(self):
        self.index.reserve("c", 1, video(uid="A"))
        self.index.release("c", 1)
        self.assertIsNone(self.index.find("c", 2, video(uid="A")))

    def test_seeded_file_on_disk_matches_by_size_and_extension(self):
        with tempfile.TemporaryDirectory() as root:
            month = os.path.join(root, "2024_03")
            os.makedirs(month)
            with open(os.path.join(month, "7 - old.mp4"), "wb") as f:
                f.truncate(2 * MB)
            self.index.seed("c", root)
        # duration is unknown for files on disk, so any duration matches
        self.assertIsNotNone(self.index.find("c", 9, video(size=2 * MB, duration=5)))
        self.assertIsNone(
            self.index.find("c", 9, video(size=2 * MB, duration=5, ext=".mkv"))
        )

    def test_seed_missing_dir_is_noop(self):
        self.index.seed("c", "/nonexistent/dir/for/test")
        self.assertIsNone(self.index.find("c", 9, video()))

    def test_commit_persists_and_reloads(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "media_index.json")
            index = MediaIndex(path, min_size=MB)
            index.reserve("c", 1, video(uid="A"))
            index.commit("c", 1)
            with open(path, encoding="utf-8") as f:
                self.assertIn("A", json.dumps(json.load(f)))

            reloaded = MediaIndex(path, min_size=MB)
            self.assertIsNotNone(reloaded.find("c", 2, video(uid="A")))

    def test_corrupt_file_starts_empty(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "media_index.json")
            with open(path, "w", encoding="utf-8") as f:
                f.write("{not json")
            index = MediaIndex(path, min_size=MB)
            self.assertIsNone(index.find("c", 2, video(uid="A")))
