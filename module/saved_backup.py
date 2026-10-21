"""Back up downloaded month folders to Telegram Saved Messages.

Saved Messages has no folders, so each file is sent with a caption whose
first line is its local path, ``<chat folder>/<month folder>/<file name>``,
followed by ``#<chat folder> #<month folder>`` so a tag search lists a
chat or a month.

A month counts as synced once every file in it is found in Saved Messages.
Its fingerprint (file names and sizes) is remembered; when the local folder
changes, Saved Messages is searched again and only missing files are sent.
Local files are never deleted here.
"""
import asyncio
import hashlib
import json
import os
import re
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

import pyrogram
from loguru import logger

from utils.format import validate_title

SAVED_MESSAGES = "me"
STATE_FILE_NAME = "saved_backup.json"
# pause between passes while the download run is still adding files
PASS_INTERVAL = 30


def hashtag(text: str) -> str:
    """Telegram hashtags stop at spaces and punctuation, so keep word chars only."""
    return "#" + (re.sub(r"\W+", "_", text).strip("_") or "_")


def caption_for(folder: str, month_dir: str, name: str) -> str:
    """Caption of a backed-up file: its path, then the chat and month tags."""
    return f"{folder}/{month_dir}/{name}\n{hashtag(folder)} {hashtag(month_dir)}"


def month_dirs(
    chat_root: str, date_format: str, end_month: datetime
) -> List[Tuple[str, str]]:
    """(month folder name, path) up to and including end_month, oldest first.

    Folders whose name does not parse with date_format are ignored.
    """
    if not os.path.isdir(chat_root):
        return []
    found = []
    for name in os.listdir(chat_root):
        path = os.path.join(chat_root, name)
        if not os.path.isdir(path):
            continue
        try:
            month = datetime.strptime(name, date_format)
        except ValueError:
            continue
        if (month.year, month.month) <= (end_month.year, end_month.month):
            found.append((month, name, path))
    found.sort()
    return [(name, path) for _, name, path in found]


def _sort_key(name: str) -> Tuple[int, str]:
    """Downloaded files start with the message id: order by it, then by name."""
    match = re.match(r"\d+", name)
    return (int(match.group()) if match else -1, name)


def month_files(path: str) -> List[str]:
    """Files directly in a month folder, oldest message first."""
    names = [
        n
        for n in os.listdir(path)
        if not n.startswith(".") and os.path.isfile(os.path.join(path, n))
    ]
    return sorted(names, key=_sort_key)


def fingerprint(path: str, names: List[str]) -> str:
    """Changes whenever a file is added, removed or resized."""
    digest = hashlib.sha1()
    for name in sorted(names):
        size = os.path.getsize(os.path.join(path, name))
        digest.update(f"{name}\0{size}\n".encode())
    return digest.hexdigest()


class BackupState:
    """What has been backed up, kept as JSON.

    ``months``: {chat key: {month folder: fingerprint}} of synced months.
    ``sent``: {chat key: {month folder: [file names]}} sent so far. Telegram's
    search can lag behind a fresh upload, so a month retried right after a
    failure checks this too instead of trusting the search alone.

    Deleting a synced month folder locally leaves both untouched: the month
    still shows as synced and is simply no longer scanned.
    """

    def __init__(self, path: str):
        self.path = path
        self.months: Dict[str, Dict[str, str]] = {}
        self.sent: Dict[str, Dict[str, List[str]]] = {}
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                self.months = data.get("months") or {}
                self.sent = data.get("sent") or {}
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as e:
            logger.warning(f"ignoring unreadable {path}: {e}")

    def _save(self):
        tmp = f"{self.path}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(
                {"months": self.months, "sent": self.sent},
                f,
                ensure_ascii=False,
                indent=1,
            )
        os.replace(tmp, self.path)

    def get(self, chat_key: str, month_dir: str) -> Optional[str]:
        """The fingerprint the month had when it was last synced."""
        return self.months.get(chat_key, {}).get(month_dir)

    def set(self, chat_key: str, month_dir: str, value: str):
        """Mark the month synced with this fingerprint and save."""
        self.months.setdefault(chat_key, {})[month_dir] = value
        self._save()

    def sent_names(self, chat_key: str, month_dir: str) -> Set[str]:
        """Files of the month this backup has sent."""
        return set(self.sent.get(chat_key, {}).get(month_dir, []))

    def add_sent(self, chat_key: str, month_dir: str, name: str):
        """Record one sent file right away, so a crash cannot make it resend."""
        names = self.sent.setdefault(chat_key, {}).setdefault(month_dir, [])
        if name not in names:
            names.append(name)
        self._save()

    def synced(self, chat_key: str) -> List[str]:
        """Synced month folders, oldest first."""
        return sorted(self.months.get(chat_key, {}))


async def remote_names(
    client: pyrogram.Client, folder: str, month_dir: str
) -> Set[str]:
    """File names of the month already in Saved Messages, read from captions."""
    prefix = f"{folder}/{month_dir}/"
    names = set()
    async for message in client.search_messages(  # type: ignore
        SAVED_MESSAGES, query=hashtag(month_dir)
    ):
        first_line = (message.caption or "").split("\n", 1)[0]
        if first_line.startswith(prefix):
            names.add(first_line[len(prefix) :])
    return names


async def upload(client: pyrogram.Client, path: str, caption: str):
    """Send one file as a document, so photos and videos keep full quality."""
    while True:
        try:
            await client.send_document(
                SAVED_MESSAGES,
                path,
                caption=caption,
                parse_mode=pyrogram.enums.ParseMode.DISABLED,
            )
            return
        except pyrogram.errors.FloodWait as wait_err:
            logger.warning(f"backup {path}: FloodWait {wait_err.value}s")
            await asyncio.sleep(wait_err.value + 1)


class SavedBackup:
    """Runs the backup passes for every chat with backup_to_saved set."""

    def __init__(self, app: Any, state_path: str):
        self.app = app
        self.state = BackupState(state_path)
        # chat key -> what is being synced right now, for the GUI
        self.current: Dict[str, str] = {}
        self._folders: Dict[str, str] = {}

    async def _folder(self, client: pyrogram.Client, chat_id) -> str:
        """The chat's download folder name, as download_media names it."""
        key = str(chat_id)
        if key not in self._folders:
            chat = await client.get_chat(chat_id)
            self._folders[key] = validate_title(chat.title or key)
        return self._folders[key]

    async def sync_chat(self, client: pyrogram.Client, chat_id, chat_config):
        """Sync this chat's months up to its backup_end_month, oldest first."""
        key = str(chat_id)
        folder = await self._folder(client, chat_id)
        root = os.path.join(self.app.save_path, folder)
        for month_dir, path in month_dirs(
            root, self.app.date_format, chat_config.backup_end_month
        ):
            names = month_files(path)
            current = fingerprint(path, names)
            if self.state.get(key, month_dir) == current:
                continue
            uploaded = await remote_names(client, folder, month_dir)
            uploaded |= self.state.sent_names(key, month_dir)
            missing = [n for n in names if n not in uploaded]
            await self._upload_month(client, key, folder, month_dir, path, missing)
            if not self.app.is_running:
                return
            self.state.set(key, month_dir, current)
            self.current.pop(key, None)

    async def _upload_month(
        self, client, key: str, folder: str, month_dir: str, path: str, names
    ):
        """Send the month's files, max_backup_task at a time, in message order.

        One failed file fails the month: the rest are cancelled and the
        month stays unsynced, so the next pass finds what is still missing.
        """
        limit = asyncio.Semaphore(max(1, self.app.max_backup_task))
        done = 0

        async def send(name: str):
            nonlocal done
            async with limit:
                if not self.app.is_running:
                    return
                await upload(
                    client,
                    os.path.join(path, name),
                    caption_for(folder, month_dir, name),
                )
                self.state.add_sent(key, month_dir, name)
                done += 1
                self.current[key] = f"{month_dir} {done}/{len(names)}"
                logger.info(f"backed up {folder}/{month_dir}/{name}")

        if names:
            self.current[key] = f"{month_dir} 0/{len(names)}"
        tasks = [asyncio.ensure_future(send(name)) for name in names]
        try:
            await asyncio.gather(*tasks)
        except BaseException:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise

    async def run(self, client: pyrogram.Client, finished: Callable[[], bool]):
        """Pass over every chat until the download run is finished or stopped."""
        if self.app.file_path_prefix[:2] != ["chat_title", "media_datetime"]:
            if any(c.backup_to_saved for c in self.app.chat_download_config.values()):
                logger.warning(
                    "backup to Saved Messages needs file_path_prefix to start "
                    "with chat_title, media_datetime; skipped"
                )
            return
        while self.app.is_running:
            last_pass = finished()
            for chat_id, chat_config in list(self.app.chat_download_config.items()):
                if not chat_config.backup_to_saved or not self.app.is_running:
                    continue
                try:
                    await self.sync_chat(client, chat_id, chat_config)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.exception(f"backup of {chat_id} failed: {e}")
                finally:
                    self.current.pop(str(chat_id), None)
            if last_pass:
                return
            await asyncio.sleep(PASS_INTERVAL)

    def status(self) -> Dict[str, Dict[str, Any]]:
        """{chat key: {"synced": [month folders], "current": text}} for the GUI."""
        return {
            str(chat_id): {
                "synced": self.state.synced(str(chat_id)),
                "current": self.current.get(str(chat_id), ""),
            }
            for chat_id, c in self.app.chat_download_config.items()
            if c.backup_to_saved
        }
