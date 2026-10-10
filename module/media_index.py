"""Per-chat index of downloaded media, used to skip duplicate files.

A file counts as a duplicate of one already downloaded (or downloading) in
the same chat when either:

* its Telegram ``file_unique_id`` matches: the same upload, forwarded or
  reposted; or
* it is at least ``min_size`` bytes and its exact byte size and extension
  match, and its duration matches too when both durations are known: the
  same file uploaded again. Exact sizes only: near-equal sizes would merge
  different episodes of a series, and missing a file is worse than
  downloading a copy.

Files already on disk from earlier runs have no recorded unique id, so
`seed` indexes them by size and extension (duration unknown).
"""
import json
import os
from dataclasses import asdict, dataclass
from typing import Dict, Optional, Tuple

from loguru import logger

_SizeKey = Tuple[int, str]


@dataclass
class MediaInfo:
    """What the index compares, all known before downloading."""

    unique_id: Optional[str]
    size: int
    duration: Optional[int]
    ext: str

    @classmethod
    def from_media(cls, media_obj, file_name: str) -> "MediaInfo":
        """Build from a pyrogram media object and its target file name."""
        return cls(
            unique_id=getattr(media_obj, "file_unique_id", None) or None,
            size=getattr(media_obj, "file_size", 0) or 0,
            duration=getattr(media_obj, "duration", None) or None,
            ext=os.path.splitext(file_name)[1].lower(),
        )


class _ChatIndex:
    """Lookup tables for one chat; a value names the matching source."""

    def __init__(self):
        self.records: Dict[int, MediaInfo] = {}
        self.reserved: Dict[int, MediaInfo] = {}
        self.disk_sizes: Dict[_SizeKey, str] = {}
        self.seeded = False


class MediaIndex:
    """Duplicate detection across a chat's messages, persisted as JSON."""

    def __init__(self, path: Optional[str], min_size: int):
        self.path = path
        self.min_size = min_size
        self._chats: Dict[str, _ChatIndex] = {}
        self._load()

    def _chat(self, chat_key: str) -> _ChatIndex:
        """The tables for a chat, created on first use."""
        return self._chats.setdefault(str(chat_key), _ChatIndex())

    def _size_key(self, info: MediaInfo) -> Optional[_SizeKey]:
        """Key for the size rule, or None when the file is too small."""
        if self.min_size <= 0 or info.size < self.min_size:
            return None
        return info.size, info.ext

    def find(self, chat_key, message_id: int, info: MediaInfo) -> Optional[str]:
        """Describe what `info` duplicates in the chat, or None."""
        chat = self._chat(chat_key)
        size_key = self._size_key(info)
        for source in (chat.records, chat.reserved):
            for other_id, other in source.items():
                if other_id == message_id:
                    continue
                if info.unique_id and info.unique_id == other.unique_id:
                    return f"message {other_id} (same file)"
                if (
                    size_key
                    and size_key == self._size_key(other)
                    and (
                        not info.duration
                        or not other.duration
                        or info.duration == other.duration
                    )
                ):
                    return f"message {other_id} (same size and duration)"
        if size_key and size_key in chat.disk_sizes:
            return f"{chat.disk_sizes[size_key]} (same size)"
        return None

    def seed(self, chat_key, root: str):
        """Index files already under `root` by size, once per chat per run."""
        chat = self._chat(chat_key)
        if chat.seeded:
            return
        chat.seeded = True
        for dirpath, _, filenames in os.walk(root):
            for name in filenames:
                path = os.path.join(dirpath, name)
                try:
                    size = os.path.getsize(path)
                except OSError:
                    continue
                key = self._size_key(
                    MediaInfo(None, size, None, os.path.splitext(name)[1].lower())
                )
                if key:
                    chat.disk_sizes.setdefault(key, path)

    def reserve(self, chat_key, message_id: int, info: MediaInfo):
        """Claim a file while it downloads, so a concurrent copy is skipped."""
        self._chat(chat_key).reserved[message_id] = info

    def release(self, chat_key, message_id: int):
        """Drop a reservation after a failed download."""
        self._chat(chat_key).reserved.pop(message_id, None)

    def commit(self, chat_key, message_id: int):
        """Record a reserved file as downloaded."""
        info = self._chat(chat_key).reserved.pop(message_id, None)
        if info:
            self.add(chat_key, message_id, info)

    def add(self, chat_key, message_id: int, info: MediaInfo):
        """Record a downloaded file and persist the index."""
        self._chat(chat_key).records[message_id] = info
        self._save()

    def _load(self):
        """Read the index; an unreadable file starts it empty."""
        if not self.path or not os.path.exists(self.path):
            return
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            for chat_key, records in data.get("chats", {}).items():
                chat = self._chat(chat_key)
                for message_id, info in records.items():
                    chat.records[int(message_id)] = MediaInfo(**info)
        except (OSError, ValueError, TypeError, AttributeError) as e:
            logger.warning(f"ignoring unreadable {self.path}: {e}")
            self._chats = {}

    def _save(self):
        """Write the index through a temp file, then rename."""
        if not self.path:
            return
        data = {
            "version": 1,
            "chats": {
                chat_key: {
                    str(message_id): asdict(info)
                    for message_id, info in chat.records.items()
                }
                for chat_key, chat in self._chats.items()
                if chat.records
            },
        }
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(tmp, self.path)
        except OSError as e:
            logger.warning(f"could not save {self.path}: {e}")
