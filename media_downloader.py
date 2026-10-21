"""Downloads media from telegram."""
import asyncio
import logging
import os
import shutil
import time
from typing import List, Optional, Tuple, Union

import pyrogram
from loguru import logger
from pyrogram.types import Audio, Document, Photo, Video, VideoNote, Voice
from rich.logging import RichHandler

from module.app import Application, ChatDownloadConfig, DownloadStatus, TaskNode
from module.bot import start_download_bot, stop_download_bot
from module.download_stat import reset_download_stat, update_download_status
from module.get_chat_history_v2 import get_chat_history_v2
from module.language import _t
from module.media_index import MediaIndex, MediaInfo
from module.saved_backup import STATE_FILE_NAME as BACKUP_STATE_FILE_NAME
from module.saved_backup import SavedBackup
from module.pyrogram_extension import (
    HookClient,
    fetch_message,
    get_extension,
    record_download_status,
    report_bot_download_status,
    set_max_concurrent_transmissions,
    set_meta_data,
    update_cloud_upload_stat,
    upload_telegram_chat,
)
from module.web import init_web
from utils.format import truncate_filename, validate_title
from utils.log import LogFilter
from utils.meta import print_meta
from utils.meta_data import MetaData

logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    datefmt="[%X]",
    handlers=[RichHandler()],
)

CONFIG_NAME = "config.yaml"
DATA_FILE_NAME = "data.yaml"
APPLICATION_NAME = "media_downloader"
app = Application(CONFIG_NAME, DATA_FILE_NAME, APPLICATION_NAME)

queue: asyncio.Queue = asyncio.Queue()
RETRY_TIME_OUT = 3
# max messages Telegram returns from one get_messages call
RETRY_FETCH_BATCH = 200
_run_tasks: List[asyncio.Task] = []
MEDIA_INDEX_FILE_NAME = "media_index.json"
_media_index: Optional[MediaIndex] = None
_saved_backup: Optional[SavedBackup] = None
_backup_task: Optional[asyncio.Task] = None
# True while a run only backs up, started by start_backup()
_backup_only = False

logging.getLogger("pyrogram.session.session").addFilter(LogFilter())
logging.getLogger("pyrogram.client").addFilter(LogFilter())

logging.getLogger("pyrogram").setLevel(logging.WARNING)


def _check_download_finish(media_size: int, download_path: str, ui_file_name: str):
    """Check download task if finish

    Parameters
    ----------
    media_size: int
        The size of the downloaded resource
    download_path: str
        Resource download hold path
    ui_file_name: str
        Really show file name

    """
    download_size = os.path.getsize(download_path)
    if media_size == download_size:
        logger.success(f"{_t('Successfully downloaded')} - {ui_file_name}")
    else:
        logger.warning(
            f"{_t('Media downloaded with wrong size')}: "
            f"{download_size}, {_t('actual')}: "
            f"{media_size}, {_t('file name')}: {ui_file_name}"
        )
        os.remove(download_path)
        raise pyrogram.errors.exceptions.bad_request_400.BadRequest()


def _move_to_download_path(temp_download_path: str, download_path: str):
    """Move file to download path

    Parameters
    ----------
    temp_download_path: str
        Temporary download path

    download_path: str
        Download path

    """

    directory, _ = os.path.split(download_path)
    os.makedirs(directory, exist_ok=True)
    shutil.move(temp_download_path, download_path)


def _check_timeout(retry: int, _: int):
    """Check if message download timeout, then add message id into failed_ids

    Parameters
    ----------
    retry: int
        Retry download message times

    message_id: int
        Try to download message 's id

    """
    if retry == 2:
        return True
    return False


def _can_download(_type: str, file_formats: dict, file_format: Optional[str]) -> bool:
    """
    Check if the given file format can be downloaded.

    Parameters
    ----------
    _type: str
        Type of media object.
    file_formats: dict
        Dictionary containing the list of file_formats
        to be downloaded for `audio`, `document` & `video`
        media types
    file_format: str
        Format of the current file to be downloaded.

    Returns
    -------
    bool
        True if the file format can be downloaded else False.
    """
    if _type in ["audio", "document", "video"]:
        allowed_formats: list = file_formats[_type]
        if not file_format in allowed_formats and allowed_formats[0] != "all":
            return False
    return True


def _is_exist(file_path: str) -> bool:
    """
    Check if a file exists and it is not a directory.

    Parameters
    ----------
    file_path: str
        Absolute path of the file to be checked.

    Returns
    -------
    bool
        True if the file exists else False.
    """
    return not os.path.isdir(file_path) and os.path.exists(file_path)


# pylint: disable = R0912


async def _get_media_meta(
    chat_id: Union[int, str],
    message: pyrogram.types.Message,
    media_obj: Union[Audio, Document, Photo, Video, VideoNote, Voice],
    _type: str,
) -> Tuple[str, str, Optional[str]]:
    """Extract file name and file id from media object.

    Parameters
    ----------
    media_obj: Union[Audio, Document, Photo, Video, VideoNote, Voice]
        Media object to be extracted.
    _type: str
        Type of media object.

    Returns
    -------
    Tuple[str, str, Optional[str]]
        file_name, file_format
    """
    if _type in ["audio", "document", "video"]:
        # pylint: disable = C0301
        file_format: Optional[str] = media_obj.mime_type.split("/")[-1]  # type: ignore
    else:
        file_format = None

    file_name = None
    temp_file_name = None
    dirname = validate_title(f"{chat_id}")
    if message.chat and message.chat.title:
        dirname = validate_title(f"{message.chat.title}")

    if message.date:
        datetime_dir_name = message.date.strftime(app.date_format)
    else:
        datetime_dir_name = "0"

    if _type in ["voice", "video_note"]:
        # pylint: disable = C0209
        file_format = media_obj.mime_type.split("/")[-1]  # type: ignore
        file_save_path = app.get_file_save_path(_type, dirname, datetime_dir_name)
        file_name = "{} - {}_{}.{}".format(
            message.id,
            _type,
            media_obj.date.isoformat(),  # type: ignore
            file_format,
        )
        file_name = validate_title(file_name)
        temp_file_name = os.path.join(app.temp_save_path, dirname, file_name)

        file_name = os.path.join(file_save_path, file_name)
    else:
        file_name = getattr(media_obj, "file_name", None)
        caption = getattr(message, "caption", None)

        file_name_suffix = ".unknown"
        if not file_name:
            file_name_suffix = get_extension(
                media_obj.file_id, getattr(media_obj, "mime_type", "")
            )
        else:
            # file_name = file_name.split(".")[0]
            _, file_name_without_suffix = os.path.split(os.path.normpath(file_name))
            file_name, file_name_suffix = os.path.splitext(file_name_without_suffix)
            if not file_name_suffix:
                file_name_suffix = get_extension(
                    media_obj.file_id, getattr(media_obj, "mime_type", "")
                )

        if caption:
            caption = validate_title(caption)
            app.set_caption_name(chat_id, message.media_group_id, caption)
            app.set_caption_entities(
                chat_id, message.media_group_id, message.caption_entities
            )
        else:
            caption = app.get_caption_name(chat_id, message.media_group_id)

        if not file_name and message.photo:
            file_name = f"{message.photo.file_unique_id}"

        gen_file_name = (
            app.get_file_name(message.id, file_name, caption) + file_name_suffix
        )

        file_save_path = app.get_file_save_path(_type, dirname, datetime_dir_name)

        temp_file_name = os.path.join(app.temp_save_path, dirname, gen_file_name)

        file_name = os.path.join(file_save_path, gen_file_name)
    return truncate_filename(file_name), truncate_filename(temp_file_name), file_format


async def add_download_task(
    message: pyrogram.types.Message,
    node: TaskNode,
):
    """Add Download task"""
    if message.empty:
        return False
    node.download_status[message.id] = DownloadStatus.Downloading
    await queue.put((message, node))
    node.total_task += 1
    return True


async def save_msg_to_file(
    app, chat_id: Union[int, str], message: pyrogram.types.Message
):
    """Write message text into file"""
    dirname = validate_title(
        message.chat.title if message.chat and message.chat.title else str(chat_id)
    )
    datetime_dir_name = message.date.strftime(app.date_format) if message.date else "0"

    file_save_path = app.get_file_save_path("msg", dirname, datetime_dir_name)
    file_name = os.path.join(
        app.temp_save_path,
        file_save_path,
        f"{app.get_file_name(message.id, None, None)}.txt",
    )

    os.makedirs(os.path.dirname(file_name), exist_ok=True)

    if _is_exist(file_name):
        return DownloadStatus.SkipDownload, None

    with open(file_name, "w", encoding="utf-8") as f:
        f.write(message.text or "")

    return DownloadStatus.SuccessDownload, file_name


async def download_task(
    client: pyrogram.Client, message: pyrogram.types.Message, node: TaskNode
):
    """Download and Forward media"""

    download_status, file_name = await download_media(
        client, message, app.media_types, app.file_formats, node
    )

    if app.enable_download_txt and message.text and not message.media:
        download_status, file_name = await save_msg_to_file(app, node.chat_id, message)

    if not node.bot:
        app.set_download_id(node, message.id, download_status)

    node.download_status[message.id] = download_status

    file_size = os.path.getsize(file_name) if file_name else 0

    await upload_telegram_chat(
        client,
        node.upload_user if node.upload_user else client,
        app,
        node,
        message,
        download_status,
        file_name,
    )

    # rclone upload
    if (
        not node.upload_telegram_chat_id
        and download_status is DownloadStatus.SuccessDownload
    ):
        ui_file_name = file_name
        if app.hide_file_name:
            ui_file_name = f"****{os.path.splitext(file_name)[-1]}"
        if await app.upload_file(
            file_name, update_cloud_upload_stat, (node, message.id, ui_file_name)
        ):
            node.upload_success_count += 1

    await report_bot_download_status(
        node.bot,
        node,
        download_status,
        file_size,
    )


# pylint: disable = R0915,R0914


def _size_limit_mb(node: TaskNode) -> int:
    """The chat's own max_file_size_mb, else the global one (0 = no limit)."""
    chat_config = app.chat_download_config.get(node.chat_id)
    if chat_config and chat_config.max_file_size_mb is not None:
        return chat_config.max_file_size_mb
    return app.max_file_size_mb


def _exceeds_size_limit(media_size: int, limit_mb: int) -> bool:
    """True when a max file size is set and the file is larger."""
    return bool(limit_mb) and (media_size or 0) > limit_mb * 1024 * 1024


def _get_media_index() -> MediaIndex:
    """The duplicate index, kept next to data.yaml and loaded on first use."""
    global _media_index  # pylint: disable = W0603
    min_size = app.duplicate_min_size_mb * 1024 * 1024
    if _media_index is None:
        _media_index = MediaIndex(
            os.path.join(os.path.abspath("."), MEDIA_INDEX_FILE_NAME), min_size
        )
    # the GUI can change it between runs without restarting
    _media_index.min_size = min_size
    return _media_index


def _chat_save_root(file_name: str) -> Optional[str]:
    """The chat's own download folder, when chat_title leads the save path.

    With another prefix first (media_type, media_datetime) the chat's files
    are spread across folders shared with other chats, so none is returned.
    """
    if not app.file_path_prefix or app.file_path_prefix[0] != "chat_title":
        return None
    rel = os.path.relpath(file_name, app.save_path)
    if rel.startswith(os.pardir):
        return None
    return os.path.join(app.save_path, rel.split(os.sep)[0])


def _find_duplicate(
    chat_key: str, message: pyrogram.types.Message, info: MediaInfo, file_name: str
) -> Optional[str]:
    """What this media duplicates in the chat; claims it when it is new."""
    index = _get_media_index()
    root = _chat_save_root(file_name)
    if root:
        index.seed(chat_key, root)
    duplicate = index.find(chat_key, message.id, info)
    if not duplicate:
        index.reserve(chat_key, message.id, info)
    return duplicate


# pylint: disable = R0911,R1702
@record_download_status
async def download_media(
    client: pyrogram.client.Client,
    message: pyrogram.types.Message,
    media_types: List[str],
    file_formats: dict,
    node: TaskNode,
):
    """
    Download media from Telegram.

    Each of the files to download are retried 3 times with a
    delay of 5 seconds each.

    Parameters
    ----------
    client: pyrogram.client.Client
        Client to interact with Telegram APIs.
    message: pyrogram.types.Message
        Message object retrieved from telegram.
    media_types: list
        List of strings of media types to be downloaded.
        Ex : `["audio", "photo"]`
        Supported formats:
            * audio
            * document
            * photo
            * video
            * voice
    file_formats: dict
        Dictionary containing the list of file_formats
        to be downloaded for `audio`, `document` & `video`
        media types.

    Returns
    -------
    int
        Current message id.
    """

    # pylint: disable = R0912

    file_name: str = ""
    ui_file_name: str = ""
    task_start_time: float = time.time()
    media_size = 0
    _media = None
    message = await fetch_message(client, message)
    chat_key = str(
        message.chat.id if message.chat and message.chat.id else node.chat_id
    )
    try:
        for _type in media_types:
            _media = getattr(message, _type, None)
            if _media is None:
                continue
            file_name, temp_file_name, file_format = await _get_media_meta(
                node.chat_id, message, _media, _type
            )
            media_size = getattr(_media, "file_size", 0)

            ui_file_name = file_name
            if app.hide_file_name:
                ui_file_name = f"****{os.path.splitext(file_name)[-1]}"

            if _can_download(_type, file_formats, file_format):
                if _is_exist(file_name):
                    file_size = os.path.getsize(file_name)
                    if file_size or file_size == media_size:
                        logger.info(
                            f"id={message.id} {ui_file_name} "
                            f"{_t('already download,download skipped')}.\n"
                        )
                        if app.skip_duplicate_media:
                            _get_media_index().add(
                                chat_key,
                                message.id,
                                MediaInfo.from_media(_media, file_name),
                            )

                        return DownloadStatus.SkipDownload, None
            else:
                return DownloadStatus.SkipDownload, None

            limit_mb = _size_limit_mb(node)
            if _exceeds_size_limit(media_size, limit_mb):
                logger.info(
                    f"id={message.id} {ui_file_name} is larger than "
                    f"max_file_size_mb={limit_mb}, skipped for now.\n"
                )
                # not SkipDownload: it stays in ids_to_retry, so raising the
                # limit later downloads it on the next run
                return DownloadStatus.FailedDownload, None

            break
    except Exception as e:
        logger.error(
            f"Message[{message.id}]: "
            f"{_t('could not be downloaded due to following exception')}:\n[{e}].",
            exc_info=True,
        )
        return DownloadStatus.FailedDownload, None
    if _media is None:
        return DownloadStatus.SkipDownload, None

    message_id = message.id

    reserved = False
    if app.skip_duplicate_media:
        info = MediaInfo.from_media(_media, file_name)
        duplicate = _find_duplicate(chat_key, message, info, file_name)
        if duplicate:
            logger.info(
                f"id={message.id} {ui_file_name} duplicates {duplicate}, "
                "download skipped.\n"
            )
            return DownloadStatus.SkipDownload, None
        reserved = True

    downloaded = False
    try:
        for retry in range(3):
            try:
                temp_download_path = await client.download_media(
                    message,
                    file_name=temp_file_name,
                    progress=update_download_status,
                    progress_args=(
                        message_id,
                        ui_file_name,
                        task_start_time,
                        node,
                        client,
                    ),
                )

                if temp_download_path and isinstance(temp_download_path, str):
                    _check_download_finish(media_size, temp_download_path, ui_file_name)
                    await asyncio.sleep(0.5)
                    _move_to_download_path(temp_download_path, file_name)
                    # TODO: if not exist file size or media
                    downloaded = True
                    return DownloadStatus.SuccessDownload, file_name
            except pyrogram.errors.exceptions.bad_request_400.BadRequest:
                logger.warning(
                    f"Message[{message.id}]: "
                    f"{_t('file reference expired, refetching')}..."
                )
                await asyncio.sleep(RETRY_TIME_OUT)
                message = await fetch_message(client, message)
                if _check_timeout(retry, message.id):
                    # pylint: disable = C0301
                    logger.error(
                        f"Message[{message.id}]: "
                        f"{_t('file reference expired for 3 retries, download skipped.')}"
                    )
            except pyrogram.errors.exceptions.flood_420.FloodWait as wait_err:
                await asyncio.sleep(wait_err.value)
                logger.warning("Message[{}]: FlowWait {}", message.id, wait_err.value)
                _check_timeout(retry, message.id)
            except TypeError:
                # pylint: disable = C0301
                logger.warning(
                    f"{_t('Timeout Error occurred when downloading Message')}[{message.id}], "
                    f"{_t('retrying after')} {RETRY_TIME_OUT} {_t('seconds')}"
                )
                await asyncio.sleep(RETRY_TIME_OUT)
                if _check_timeout(retry, message.id):
                    logger.error(
                        f"Message[{message.id}]: {_t('Timing out after 3 reties, download skipped.')}"
                    )
            except Exception as e:
                # pylint: disable = C0301
                logger.error(
                    f"Message[{message.id}]: "
                    f"{_t('could not be downloaded due to following exception')}:\n[{e}].",
                    exc_info=True,
                )
                break

        return DownloadStatus.FailedDownload, None
    finally:
        # also runs when Stop cancels the task, so the claim never outlives it
        if reserved:
            if downloaded:
                _get_media_index().commit(chat_key, message_id)
            else:
                _get_media_index().release(chat_key, message_id)


def _load_config():
    """Load config"""
    app.load_config()


def _check_config() -> bool:
    """Check config"""
    print_meta(logger)
    try:
        _load_config()
        logger.add(
            os.path.join(app.log_file_path, "tdl.log"),
            rotation="10 MB",
            retention="10 days",
            level=app.log_level,
        )
    except Exception as e:
        logger.exception(f"load config error: {e}")
        return False

    if not app.api_id or not app.api_hash:
        logger.error("api_id and api_hash are required in config.yaml")
        return False

    return True


async def worker(client: pyrogram.client.Client):
    """Work for download task"""
    while app.is_running:
        try:
            item = await queue.get()
            message = item[0]
            node: TaskNode = item[1]

            if node.is_stop_transmission:
                continue

            if node.client:
                await download_task(node.client, message, node)
            else:
                await download_task(client, message, node)
        except Exception as e:
            logger.exception(f"{e}")


async def download_chat_task(
    client: pyrogram.Client,
    chat_download_config: ChatDownloadConfig,
    node: TaskNode,
):
    """Download all task"""
    messages_iter = get_chat_history_v2(
        client,
        node.chat_id,
        limit=node.limit,
        max_id=node.end_offset_id,
        offset_id=chat_download_config.last_read_message_id,
        reverse=True,
    )

    chat_download_config.node = node

    if chat_download_config.ids_to_retry:
        logger.info(f"{_t('Downloading files failed during last run')}...")
        retry_ids = list(chat_download_config.ids_to_retry)
        # Telegram returns at most RETRY_FETCH_BATCH messages per call; the rest
        # would be dropped, and the history loop skips retry ids, so fetch them all
        for start in range(0, len(retry_ids), RETRY_FETCH_BATCH):
            skipped_messages: list = await client.get_messages(  # type: ignore
                chat_id=node.chat_id,
                message_ids=retry_ids[start : start + RETRY_FETCH_BATCH],
            )

            for message in skipped_messages:
                if app.is_before_start(
                    chat_download_config, getattr(message, "date", None)
                ):
                    # dropped from ids_to_retry when progress is saved
                    node.download_status[message.id] = DownloadStatus.SkipDownload
                    continue
                await add_download_task(message, node)

    async for message in messages_iter:  # type: ignore
        meta_data = MetaData()

        caption = message.caption
        if caption:
            caption = validate_title(caption)
            app.set_caption_name(node.chat_id, message.media_group_id, caption)
            app.set_caption_entities(
                node.chat_id, message.media_group_id, message.caption_entities
            )
        else:
            caption = app.get_caption_name(node.chat_id, message.media_group_id)
        set_meta_data(meta_data, message, caption)

        if app.need_skip_message(chat_download_config, message.id):
            continue

        if app.exec_filter(chat_download_config, meta_data):
            await add_download_task(message, node)
        else:
            node.download_status[message.id] = DownloadStatus.SkipDownload
            if message.media_group_id:
                await upload_telegram_chat(
                    client,
                    node.upload_user,
                    app,
                    node,
                    message,
                    DownloadStatus.SkipDownload,
                )

    chat_download_config.need_check = True
    chat_download_config.total_task = node.total_task
    node.is_running = True


async def download_all_chat(client: pyrogram.Client):
    """Download All chat"""
    for key, value in app.chat_download_config.items():
        value.node = TaskNode(chat_id=key)
        try:
            await download_chat_task(client, value, value.node)
        except Exception as e:
            logger.warning(f"Download {key} error: {e}")
        finally:
            value.need_check = True


def _all_chats_finished() -> bool:
    """True once every configured chat has been iterated and all its tasks are done."""
    for value in app.chat_download_config.values():
        if not value.need_check or value.total_task != value.finish_task:
            return False
    return True


async def run_until_all_task_finish():
    """Normal download"""
    while True:
        if (not app.bot_token and _all_chats_finished()) or app.restart_program:
            break

        await asyncio.sleep(1)


async def wait_until_finished():
    """Wait until the current run completes or is stopped (bot mode is ignored).

    The run includes the backup to Saved Messages, which makes its last pass
    once every chat has finished downloading.
    """
    while app.is_running and (
        (not _backup_only and not _all_chats_finished())
        or (_backup_task and not _backup_task.done())
    ):
        await asyncio.sleep(1)


def run_phase() -> str:
    """ "download" while chats are downloading, "backup" once only backup runs."""
    if _backup_only or _all_chats_finished():
        return "backup"
    return "download"


def _get_saved_backup() -> SavedBackup:
    """The Saved Messages backup, its state kept next to data.yaml."""
    global _saved_backup  # pylint: disable = W0603
    if _saved_backup is None:
        _saved_backup = SavedBackup(
            app, os.path.join(os.path.abspath("."), BACKUP_STATE_FILE_NAME)
        )
    return _saved_backup


def backup_status() -> dict:
    """Synced months and current progress of every chat backed up."""
    return _get_saved_backup().status()


def upload_status() -> dict:
    """Files being uploaded to Saved Messages right now, with progress."""
    return _get_saved_backup().upload_status()


def _set_transmissions(client: pyrogram.Client):
    """Let pyrogram run as many uploads and downloads as this run asks for.

    pyrogram allows one transfer at a time unless told otherwise, which would
    turn max_backup_task parallel uploads back into a queue.
    """
    set_max_concurrent_transmissions(
        client, max(app.max_concurrent_transmissions, app.max_backup_task)
    )


async def start_download(client: pyrogram.Client):
    """Start a download run for every chat in app.chat_download_config.

    Safe to call again after stop_download(): per-run state (queue, progress,
    worker tasks, transmission semaphores) is rebuilt on the running loop.
    """
    # pylint: disable = W0603
    global queue, _backup_task, _backup_only
    _backup_only = False
    reset_download_stat()
    queue = asyncio.Queue()
    app.is_running = True
    _set_transmissions(client)
    _run_tasks.clear()
    _run_tasks.append(asyncio.ensure_future(download_all_chat(client)))
    for _ in range(app.max_download_task):
        _run_tasks.append(asyncio.ensure_future(worker(client)))
    _backup_task = asyncio.ensure_future(
        _get_saved_backup().run(client, _all_chats_finished)
    )
    _run_tasks.append(_backup_task)


async def start_backup(client: pyrogram.Client):
    """Start a run that only backs up to Saved Messages, one pass per chat.

    Nothing is downloaded; stop_download() stops it like a download run.
    """
    global _backup_task, _backup_only  # pylint: disable = W0603
    reset_download_stat()
    app.is_running = True
    _backup_only = True
    _set_transmissions(client)
    _run_tasks.clear()
    _backup_task = asyncio.ensure_future(_get_saved_backup().run(client, lambda: True))
    _run_tasks.append(_backup_task)


async def stop_download():
    """Stop the current run immediately and persist progress for the next run.

    Cancels download_all_chat and every worker right away instead of waiting
    for them to notice app.is_running: idle workers block in queue.get() and
    would otherwise never see the flag, stalling every stop.
    """
    app.is_running = False
    for value in app.chat_download_config.values():
        value.node.stop_transmission()
    if _run_tasks:
        for task in _run_tasks:
            task.cancel()
        await asyncio.gather(*_run_tasks, return_exceptions=True)
        _run_tasks.clear()
    app.update_config()
    # the run is over: empty the downloading list instead of leaving it frozen
    reset_download_stat()


def _exec_loop():
    """Exec loop"""

    app.loop.run_until_complete(run_until_all_task_finish())


async def start_server(client: pyrogram.Client):
    """
    Start the server using the provided client.
    """
    await client.start()


async def stop_server(client: pyrogram.Client):
    """
    Stop the server using the provided client.
    """
    await client.stop()


def main():
    """Main function of the downloader."""
    client = HookClient(
        "media_downloader",
        api_id=app.api_id,
        api_hash=app.api_hash,
        proxy=app.proxy,
        workdir=app.session_file_path,
        start_timeout=app.start_timeout,
        no_updates=True,
    )
    try:
        app.pre_run()
        init_web(app)

        app.loop.run_until_complete(start_server(client))
        logger.success(_t("Successfully started (Press Ctrl+C to stop)"))

        app.loop.run_until_complete(start_download(client))

        if app.bot_token:
            app.loop.run_until_complete(
                start_download_bot(app, client, add_download_task, download_chat_task)
            )
        _exec_loop()
    except KeyboardInterrupt:
        logger.info(_t("KeyboardInterrupt"))
    except Exception as e:
        logger.exception("{}", e)
    finally:
        app.is_running = False
        if app.bot_token:
            app.loop.run_until_complete(stop_download_bot())
        logger.info(f"{_t('update config')}......")
        # cancels the workers and writes last_read_message_id / ids_to_retry
        app.loop.run_until_complete(stop_download())
        app.loop.run_until_complete(stop_server(client))
        logger.info(_t("Stopped!"))
        # check_for_updates(app.proxy)
        logger.success(
            f"{_t('Updated last read message_id to config file')},"
            f"{_t('total download')} {app.total_download_task}, "
            f"{_t('total upload file')} "
            f"{app.cloud_drive_config.total_upload_success_file_count}"
        )


if __name__ == "__main__":
    if _check_config():
        main()
