"""Backing up downloaded month folders to Saved Messages"""
import asyncio
import os
import shutil
import tempfile
import unittest
from datetime import datetime
from types import SimpleNamespace

from module.saved_backup import (
    BackupState,
    SavedBackup,
    caption_for,
    fingerprint,
    hashtag,
    month_dirs,
    month_files,
)


def _write(path, size=1):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(b"x" * size)


class FakeClient:
    """Saved Messages kept as a list of captions."""

    def __init__(self, title="My Chat"):
        self.title = title
        self.captions = []

    async def get_chat(self, chat_id):
        return SimpleNamespace(title=self.title)

    async def search_messages(self, chat_id, query):
        assert chat_id == "me"
        for caption in list(self.captions):
            if query in caption.split():
                yield SimpleNamespace(caption=caption)

    async def send_document(self, chat_id, path, caption, parse_mode):
        assert chat_id == "me" and os.path.isfile(path)
        self.captions.append(caption)


class HelpersTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_hashtag_and_caption(self):
        self.assertEqual(hashtag("电影 频道-1"), "#电影_频道_1")
        self.assertEqual(
            caption_for("My Chat", "2026_09", "12 - a.mp4"),
            "My Chat/2026_09/12 - a.mp4\n#My_Chat #2026_09",
        )

    def test_month_dirs_up_to_end_month_oldest_first(self):
        for name in ["2026_10", "2026_09", "2025_12", "notes", "2026_01"]:
            os.makedirs(os.path.join(self.tmp, name))
        _write(os.path.join(self.tmp, "2026_08"))  # a file, not a folder
        found = month_dirs(self.tmp, "%Y_%m", datetime(2026, 9, 1))
        self.assertEqual([n for n, _ in found], ["2025_12", "2026_01", "2026_09"])
        self.assertEqual(
            month_dirs(os.path.join(self.tmp, "x"), "%Y_%m", datetime(2026, 9, 1)), []
        )

    def test_month_files_by_message_id(self):
        for name in ["10 - b.mp4", "9 - a.mp4", ".DS_Store", "note.txt"]:
            _write(os.path.join(self.tmp, name))
        self.assertEqual(month_files(self.tmp), ["note.txt", "9 - a.mp4", "10 - b.mp4"])

    def test_fingerprint_changes_with_files(self):
        _write(os.path.join(self.tmp, "1.mp4"))
        first = fingerprint(self.tmp, ["1.mp4"])
        _write(os.path.join(self.tmp, "1.mp4"), size=2)
        self.assertNotEqual(fingerprint(self.tmp, ["1.mp4"]), first)

    def test_state_round_trip(self):
        path = os.path.join(self.tmp, "state.json")
        state = BackupState(path)
        state.set("-100", "2026_09", "abc")
        state.set("-100", "2026_01", "def")
        again = BackupState(path)
        self.assertEqual(again.get("-100", "2026_09"), "abc")
        self.assertEqual(again.synced("-100"), ["2026_01", "2026_09"])

    def test_unreadable_state_starts_empty(self):
        path = os.path.join(self.tmp, "state.json")
        _write(path)
        self.assertEqual(BackupState(path).months, {})


class SyncTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.save_path = os.path.join(self.tmp, "dl")
        self.chat = SimpleNamespace(
            backup_to_saved=True, backup_end_month=datetime(2026, 9, 1)
        )
        self.app = SimpleNamespace(
            save_path=self.save_path,
            date_format="%Y_%m",
            is_running=True,
            max_backup_task=1,
            file_path_prefix=["chat_title", "media_datetime"],
            chat_download_config={-100: self.chat},
        )
        self.backup = SavedBackup(self.app, os.path.join(self.tmp, "state.json"))
        self.client = FakeClient()

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def file(self, month, name):
        _write(os.path.join(self.save_path, "My Chat", month, name))

    def sync(self):
        asyncio.run(self.backup.sync_chat(self.client, -100, self.chat))

    def test_uploads_oldest_month_first_and_skips_later_months(self):
        self.file("2026_09", "5 - c.mp4")
        self.file("2026_08", "2 - b.jpg")
        self.file("2026_08", "1 - a.mp4")
        self.file("2026_10", "9 - new.mp4")
        self.sync()
        self.assertEqual(
            [c.split("\n")[0] for c in self.client.captions],
            [
                "My Chat/2026_08/1 - a.mp4",
                "My Chat/2026_08/2 - b.jpg",
                "My Chat/2026_09/5 - c.mp4",
            ],
        )
        self.assertEqual(self.backup.status()["-100"]["synced"], ["2026_08", "2026_09"])
        # local files stay
        self.assertTrue(
            os.path.isfile(
                os.path.join(self.save_path, "My Chat", "2026_08", "1 - a.mp4")
            )
        )

    def test_unchanged_month_is_not_checked_again(self):
        self.file("2026_08", "1 - a.mp4")
        self.sync()
        self.client.captions.clear()
        self.sync()
        self.assertEqual(self.client.captions, [])

    def test_changed_month_sends_only_what_saved_messages_lacks(self):
        self.file("2026_08", "1 - a.mp4")
        self.sync()
        self.file("2026_08", "3 - late.mp4")
        self.sync()
        self.assertEqual(
            [c.split("\n")[0] for c in self.client.captions],
            ["My Chat/2026_08/1 - a.mp4", "My Chat/2026_08/3 - late.mp4"],
        )

    def test_files_already_in_saved_messages_are_not_resent(self):
        # e.g. the state file was lost, or the upload was cut off midway
        self.file("2026_08", "1 - a.mp4")
        self.file("2026_08", "2 - b.mp4")
        self.client.captions.append(caption_for("My Chat", "2026_08", "1 - a.mp4"))
        self.client.captions.append(caption_for("Other", "2026_08", "2 - b.mp4"))
        self.sync()
        self.assertEqual(
            self.client.captions[2:], [caption_for("My Chat", "2026_08", "2 - b.mp4")]
        )

    def test_stopping_leaves_the_month_unsynced(self):
        self.file("2026_08", "1 - a.mp4")
        self.app.is_running = False
        self.sync()
        self.assertEqual(self.client.captions, [])
        self.assertEqual(self.backup.status()["-100"]["synced"], [])

    def test_uploads_at_most_max_backup_task_at_once(self):
        for i in range(6):
            self.file("2026_08", f"{i} - v.mp4")
        self.app.max_backup_task = 2
        active = peak = 0
        send = self.client.send_document

        async def slow_send(*args, **kwargs):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.01)
            await send(*args, **kwargs)
            active -= 1

        self.client.send_document = slow_send
        self.sync()
        self.assertEqual(peak, 2)
        self.assertEqual(len(self.client.captions), 6)
        self.assertEqual(self.backup.status()["-100"]["synced"], ["2026_08"])

    def test_a_failed_file_leaves_the_month_unsynced(self):
        self.file("2026_08", "1 - a.mp4")
        self.file("2026_08", "2 - b.mp4")
        self.app.max_backup_task = 2
        send = self.client.send_document

        async def failing_send(chat_id, path, caption, parse_mode):
            if path.endswith("2 - b.mp4"):
                raise RuntimeError("network")
            await send(chat_id, path, caption, parse_mode)

        self.client.send_document = failing_send
        with self.assertRaises(RuntimeError):
            self.sync()
        self.assertEqual(self.backup.status()["-100"]["synced"], [])
        # the next pass sends only what is missing
        self.client.send_document = send
        self.sync()
        self.assertEqual(
            [c.split("\n")[0] for c in self.client.captions],
            ["My Chat/2026_08/1 - a.mp4", "My Chat/2026_08/2 - b.mp4"],
        )

    def test_search_lag_does_not_resend(self):
        self.file("2026_08", "1 - a.mp4")
        self.file("2026_08", "2 - b.mp4")
        send = self.client.send_document

        async def failing_send(chat_id, path, caption, parse_mode):
            if path.endswith("2 - b.mp4"):
                raise RuntimeError("network")
            await send(chat_id, path, caption, parse_mode)

        self.client.send_document = failing_send
        with self.assertRaises(RuntimeError):
            self.sync()
        # search has not indexed "1 - a.mp4" yet
        sent = list(self.client.captions)
        self.client.captions.clear()
        self.client.send_document = send
        self.sync()
        self.assertEqual(
            [c.split("\n")[0] for c in sent + self.client.captions],
            ["My Chat/2026_08/1 - a.mp4", "My Chat/2026_08/2 - b.mp4"],
        )

    def test_month_deleted_in_saved_messages_is_not_resent(self):
        self.file("2026_08", "1 - a.mp4")
        self.sync()
        self.client.captions.clear()  # deleted from Saved Messages
        self.sync()
        self.assertEqual(self.client.captions, [])
        # even when the local month later gets a new file, only that is sent
        self.file("2026_08", "2 - new.mp4")
        self.sync()
        self.assertEqual(
            [c.split("\n")[0] for c in self.client.captions],
            ["My Chat/2026_08/2 - new.mp4"],
        )

    def test_deleting_synced_months_locally_is_fine(self):
        self.file("2026_07", "1 - a.mp4")
        self.file("2026_08", "2 - b.mp4")
        self.file("2026_08", "3 - c.mp4")
        self.sync()
        self.client.captions.clear()
        shutil.rmtree(os.path.join(self.save_path, "My Chat", "2026_07"))
        os.remove(os.path.join(self.save_path, "My Chat", "2026_08", "3 - c.mp4"))
        self.sync()
        self.assertEqual(self.client.captions, [])
        self.assertEqual(self.backup.status()["-100"]["synced"], ["2026_07", "2026_08"])

    def test_search_lag_does_not_resend(self):
        self.file("2026_08", "1 - a.mp4")
        self.file("2026_08", "2 - b.mp4")
        send = self.client.send_document

        async def failing_send(chat_id, path, caption, parse_mode):
            if path.endswith("2 - b.mp4"):
                raise RuntimeError("network")
            await send(chat_id, path, caption, parse_mode)

        self.client.send_document = failing_send
        with self.assertRaises(RuntimeError):
            self.sync()
        # search has not indexed "1 - a.mp4" yet
        sent = list(self.client.captions)
        self.client.captions.clear()
        self.client.send_document = send
        self.sync()
        self.assertEqual(
            [c.split("\n")[0] for c in sent + self.client.captions],
            ["My Chat/2026_08/1 - a.mp4", "My Chat/2026_08/2 - b.mp4"],
        )

    def test_deleting_a_synced_month_locally(self):
        self.file("2026_07", "1 - a.mp4")
        self.file("2026_08", "2 - b.mp4")
        self.sync()
        shutil.rmtree(os.path.join(self.save_path, "My Chat", "2026_07"))
        self.sync()
        self.assertEqual(len(self.client.captions), 2)
        self.assertEqual(self.backup.status()["-100"]["synced"], ["2026_07", "2026_08"])

    def test_deleting_some_files_of_a_synced_month_locally(self):
        self.file("2026_08", "1 - a.mp4")
        self.file("2026_08", "2 - b.mp4")
        self.sync()
        os.remove(os.path.join(self.save_path, "My Chat", "2026_08", "2 - b.mp4"))
        self.sync()
        self.assertEqual(len(self.client.captions), 2)
        self.assertEqual(self.backup.status()["-100"]["synced"], ["2026_08"])

    def test_sent_record_survives_a_restart(self):
        self.file("2026_08", "1 - a.mp4")
        self.backup.state.add_sent("-100", "2026_08", "1 - a.mp4")
        again = SavedBackup(self.app, self.backup.state.path)
        asyncio.run(again.sync_chat(self.client, -100, self.chat))
        self.assertEqual(self.client.captions, [])
        self.assertEqual(again.status()["-100"]["synced"], ["2026_08"])

    def test_status_lists_only_backed_up_chats(self):
        self.app.chat_download_config[-200] = SimpleNamespace(backup_to_saved=False)
        self.assertEqual(list(self.backup.status()), ["-100"])

    def test_run_needs_chat_title_then_month_folders(self):
        self.app.file_path_prefix = ["media_datetime", "chat_title"]
        self.file("2026_08", "1 - a.mp4")
        asyncio.run(self.backup.run(self.client, lambda: True))
        self.assertEqual(self.client.captions, [])

    def test_run_makes_a_last_pass_when_downloads_finished(self):
        self.file("2026_08", "1 - a.mp4")
        asyncio.run(self.backup.run(self.client, lambda: True))
        self.assertEqual(len(self.client.captions), 1)


if __name__ == "__main__":
    unittest.main()
