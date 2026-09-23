"""Pure helpers for the GUI: validation, parsing and config.yaml persistence.

Nothing here talks to Telegram or touches threads, so it is easy to unit test.
"""

import os
import re
import shutil
import time
from typing import Any, Dict, Iterable, List, Optional, Union

from ruamel import yaml

from module.app import DEFAULT_FILE_FORMATS, DEFAULT_MEDIA_TYPES

_yaml = yaml.YAML()

ChatId = Union[int, str]

MAX_DOWNLOAD_TASK_MIN = 1
MAX_DOWNLOAD_TASK_MAX = 10
PROXY_SCHEMES = ("socks5", "http")
PHONE_ERROR = "手机号格式不正确，请带国家区号，例如 +8613800000000"

_LINK_PREFIX = re.compile(r"^(?:https?://)?(?:www\.)?(?:t|telegram)\.me/(?:s/)?", re.I)
_USERNAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]{3,31}$")
_API_HASH = re.compile(r"^[0-9a-f]{32}$")
_PHONE = re.compile(r"^\+?\d{6,15}$")


class GuiError(Exception):
    """An error whose message is shown to the user as-is."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


class InvalidState(GuiError):
    """The requested operation is not allowed in the current state."""

    def __init__(self, message: str = "当前状态下不能执行这个操作"):
        super().__init__(message, status=409)


def _load(path: str) -> Any:
    """Read a YAML file; an empty file counts as an empty mapping."""
    with open(path, encoding="utf-8") as f:
        data = _yaml.load(f)
    return data if data is not None else {}


def _dump(path: str, data: Any) -> None:
    """Write YAML via write-then-rename so a crash never leaves a truncated config."""
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        _yaml.dump(data, f)
    os.replace(tmp_path, path)


def default_config(save_path: str) -> Dict[str, Any]:
    """Config written on first launch; credentials are left for the user."""
    return {
        "api_id": "",
        "api_hash": "",
        "chat": [],
        "media_types": list(DEFAULT_MEDIA_TYPES),
        "file_formats": {k: list(v) for k, v in DEFAULT_FILE_FORMATS.items()},
        "save_path": save_path,
        "file_path_prefix": ["chat_title", "media_datetime"],
    }


def ensure_config_file(path: str, save_path: str) -> Optional[str]:
    """Create config.yaml with defaults if missing, empty or unreadable.

    A config that exists but cannot be parsed is backed up first. Returns a
    notice for the user when that happens, otherwise None.
    """
    notice = None
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                data = _yaml.load(f)
        except Exception:
            data = False
        if isinstance(data, dict):
            return None
        if data is not None:
            backup = f"{path}.broken-{time.strftime('%Y%m%d-%H%M%S')}"
            shutil.copyfile(path, backup)
            notice = f"配置文件损坏，已备份为 {os.path.basename(backup)} 并恢复默认配置"
    _dump(path, default_config(save_path))
    return notice


def _validate_proxy(raw: Any) -> Optional[Dict[str, Any]]:
    """Normalize the proxy part of the form; None means no proxy."""
    if not raw:
        return None
    scheme = str(raw.get("scheme", "")).strip().lower()
    if scheme not in PROXY_SCHEMES:
        raise GuiError("代理类型只能是 SOCKS5 或 HTTP")
    hostname = str(raw.get("hostname", "")).strip()
    if not hostname:
        raise GuiError("请填写代理地址")
    try:
        port = int(raw.get("port"))
    except (TypeError, ValueError):
        port = 0
    if not 1 <= port <= 65535:
        raise GuiError("代理端口应在 1 到 65535 之间")
    proxy: Dict[str, Any] = {"scheme": scheme, "hostname": hostname, "port": port}
    username = str(raw.get("username") or "").strip()
    password = str(raw.get("password") or "")
    # Pyrogram would try to authenticate with an empty username, so only keep real ones
    if username:
        proxy["username"] = username
        if password:
            proxy["password"] = password
    return proxy


def _validate_save_path(raw: Any) -> str:
    """Expand ~, create the folder and make sure it is writable."""
    text = str(raw or "").strip()
    if not text:
        raise GuiError("请选择保存目录")
    path = os.path.abspath(os.path.expanduser(text))
    try:
        os.makedirs(path, exist_ok=True)
    except OSError as e:
        raise GuiError(f"无法创建保存目录：{e.strerror}") from e
    if not os.access(path, os.W_OK):
        raise GuiError("保存目录没有写入权限，请换一个目录")
    return path


def validate_basic_config(form: Dict[str, Any]) -> Dict[str, Any]:
    """Validate the settings form and return normalized values."""
    try:
        api_id = int(str(form.get("api_id", "")).strip())
    except ValueError:
        api_id = 0
    if api_id <= 0:
        raise GuiError("api_id 应该是一串数字")

    api_hash = str(form.get("api_hash", "")).strip().lower()
    if not _API_HASH.match(api_hash):
        raise GuiError("api_hash 应该是 32 位的字母和数字组合")

    media_types = form.get("media_types")
    if (
        not isinstance(media_types, list)
        or not media_types
        or any(item not in DEFAULT_MEDIA_TYPES for item in media_types)
    ):
        raise GuiError("请至少选择一种媒体类型")

    try:
        max_download_task = int(form.get("max_download_task", 5))
    except (TypeError, ValueError):
        max_download_task = 0
    if not MAX_DOWNLOAD_TASK_MIN <= max_download_task <= MAX_DOWNLOAD_TASK_MAX:
        raise GuiError(
            f"同时下载文件数应在 {MAX_DOWNLOAD_TASK_MIN} 到 {MAX_DOWNLOAD_TASK_MAX} 之间"
        )

    return {
        "api_id": api_id,
        "api_hash": api_hash,
        "proxy": _validate_proxy(form.get("proxy")),
        "save_path": _validate_save_path(form.get("save_path")),
        "media_types": [item for item in DEFAULT_MEDIA_TYPES if item in media_types],
        "max_download_task": max_download_task,
    }


def write_basic_config(path: str, basic: Dict[str, Any]) -> None:
    """Update only the basic keys; comments and advanced keys are kept."""
    data = _load(path)
    for key in ("api_id", "api_hash", "save_path", "media_types", "max_download_task"):
        data[key] = basic[key]
    if basic["proxy"]:
        data["proxy"] = basic["proxy"]
    elif "proxy" in data:
        del data["proxy"]
    _dump(path, data)


def read_basic_config(app: Any) -> Dict[str, Any]:
    """The basic settings as the settings form expects them."""
    return {
        "api_id": app.api_id or "",
        "api_hash": app.api_hash or "",
        "proxy": dict(app.proxy) if app.proxy else None,
        "save_path": app.save_path,
        "media_types": list(app.media_types),
        "max_download_task": app.max_download_task,
    }


def read_chats(path: str) -> List[Any]:
    """The `chat` list from config.yaml, understanding the legacy single-chat format."""
    data = _load(path)
    if data.get("chat"):
        return list(data["chat"])
    if data.get("chat_id"):
        return [
            {
                "chat_id": data["chat_id"],
                "last_read_message_id": data.get("last_read_message_id", 0),
            }
        ]
    return []


def write_chats(path: str, chats: List[Any]) -> None:
    """Replace the `chat` list and drop the legacy single-chat keys."""
    data = _load(path)
    data["chat"] = chats
    for key in ("chat_id", "last_read_message_id"):
        if key in data:
            del data[key]
    _dump(path, data)


def _norm(value: Any) -> str:
    """Compare chat ids and usernames case-insensitively, ignoring a leading @."""
    return str(value).strip().lstrip("@").lower()


def merge_chats(
    existing: List[Any], chat_ids: List[ChatId], usernames: Dict[ChatId, str]
) -> List[Any]:
    """Rebuild config['chat'] in `chat_ids` order, keeping matching entries intact.

    An existing entry matches by chat id or, for configs written with usernames,
    by the chat's username. New chats start from the first message.
    """
    remaining = list(existing)
    result: List[Any] = []
    for chat_id in chat_ids:
        wanted = {_norm(chat_id)}
        if usernames.get(chat_id):
            wanted.add(_norm(usernames[chat_id]))
        match = next(
            (item for item in remaining if _norm(item.get("chat_id", "")) in wanted),
            None,
        )
        if match is None:
            result.append({"chat_id": chat_id, "last_read_message_id": 0})
        else:
            remaining.remove(match)
            result.append(match)
    return result


def find_chat(
    chats: Iterable[Dict[str, Any]], chat_id: ChatId
) -> Optional[Dict[str, Any]]:
    """Find a described chat by numeric id or username."""
    wanted = _norm(chat_id)
    for chat in chats:
        if _norm(chat["id"]) == wanted:
            return chat
        if chat.get("username") and _norm(chat["username"]) == wanted:
            return chat
    return None


def parse_chat_link(text: str) -> str:
    """Extract a public username from t.me links, @name or a bare name."""
    value = _LINK_PREFIX.sub("", (text or "").strip())
    if value.startswith("+") or value.lower().startswith("joinchat/"):
        raise GuiError("暂不支持私有邀请链接，请先在 Telegram 里加入该群或频道，之后它会出现在上面的列表里")
    value = value.lstrip("@").split("/")[0].split("?")[0]
    if not _USERNAME.match(value):
        raise GuiError("无法识别的链接，请粘贴形如 t.me/xxx 或 @xxx 的公开链接")
    return value


def normalize_phone(text: str) -> str:
    """Strip spaces, dashes and brackets; require 6-15 digits with optional +."""
    phone = re.sub(r"[\s\-()]", "", text or "")
    if not _PHONE.match(phone):
        raise GuiError(PHONE_ERROR)
    return phone
